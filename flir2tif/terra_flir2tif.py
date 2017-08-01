#!/usr/bin/env python

import os
import logging
import shutil
import datetime
import numpy

from pyclowder.extractors import Extractor
from pyclowder.utils import CheckMessage
import pyclowder.files
import pyclowder.datasets
import terrautils.extractors
import terrautils.sensors
import terrautils.metadata

import Get_FLIR as getFlir


class FlirBin2JpgTiff(Extractor):
    def __init__(self):
        Extractor.__init__(self)

        influx_host = os.getenv("INFLUXDB_HOST", "terra-logging.ncsa.illinois.edu")
        influx_port = os.getenv("INFLUXDB_PORT", 8086)
        influx_db = os.getenv("INFLUXDB_DB", "extractor_db")
        influx_user = os.getenv("INFLUXDB_USER", "terra")
        influx_pass = os.getenv("INFLUXDB_PASSWORD", "")

        # add any additional arguments to parser
        self.parser.add_argument('--overwrite', dest="force_overwrite", type=bool, nargs='?', default=False,
                                 help="whether to overwrite output file if it already exists in output directory")
        self.parser.add_argument('--scale', dest="scale_values", type=bool, nargs='?', default=True,
                                 help="scale individual flir images based on px range as opposed to full field stitch")
        self.parser.add_argument('--influxHost', dest="influx_host", type=str, nargs='?',
                                 default=influx_host, help="InfluxDB URL for logging")
        self.parser.add_argument('--influxPort', dest="influx_port", type=int, nargs='?',
                                 default=influx_port, help="InfluxDB port")
        self.parser.add_argument('--influxUser', dest="influx_user", type=str, nargs='?',
                                 default=influx_user, help="InfluxDB username")
        self.parser.add_argument('--influxPass', dest="influx_pass", type=str, nargs='?',
                                 default=influx_pass, help="InfluxDB password")
        self.parser.add_argument('--influxDB', dest="influx_db", type=str, nargs='?',
                                 default=influx_db, help="InfluxDB database")

        # parse command line and load default logging configuration
        self.setup()

        # setup logging for the exctractor
        logging.getLogger('pyclowder').setLevel(logging.DEBUG)
        logging.getLogger('__main__').setLevel(logging.DEBUG)

        # assign other arguments
        self.force_overwrite = self.args.force_overwrite
        self.scale_values = self.args.scale_values
        self.influx_params = {
            "host": self.args.influx_host,
            "port": self.args.influx_port,
            "db": self.args.influx_db,
            "user": self.args.influx_user,
            "pass": self.args.influx_pass
        }

    def check_message(self, connector, host, secret_key, resource, parameters):
        if not terrautils.extractors.is_latest_file(resource):
            return CheckMessage.ignore

        # Check for an ir.BIN file and metadata before beginning processing
        found_ir = None
        found_md = None
        for f in resource['files']:
            if 'filename' in f and f['filename'].endswith('_ir.bin'):
                found_ir = f['filepath']
            elif 'filename' in f and f['filename'].endswith('_metadata.json'):
                found_md = f['filepath']

        if found_ir:
            # Check if outputs already exist
            png_path = terrautils.sensors.get_sensor_path_by_dataset("ua-mac", "Level_1", resource['dataset_info']['name'],
                                                                     "flir2tif", 'png')
            tiff_path = terrautils.sensors.get_sensor_path_by_dataset("ua-mac", "Level_1", resource['dataset_info']['name'],
                                                                     "flir2tif", 'tif')
            if os.path.exists(png_path) and os.path.exists(tiff_path) and not self.force_overwrite:
                logging.info("skipping dataset %s, outputs already exist" % resource['id'])
                return CheckMessage.ignore

            # If we don't find _metadata.json file, check if we have metadata attached to dataset instead
            if not found_md:
                md = pyclowder.datasets.download_metadata(connector, host, secret_key, resource['id'])
                if terrautils.metadata.get_extractor_metadata(md, self.extractor_info['name']) and not self.force_overwrite:
                    logging.info("skipping dataset %s, already processed" % resource['id'])
                    return CheckMessage.ignore
                if terrautils.metadata.get_terraref_metadata(md):
                    return CheckMessage.download
                return CheckMessage.ignore
            else:
                return CheckMessage.download

        return CheckMessage.ignore

    def process_message(self, connector, host, secret_key, resource, parameters):
        starttime = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        created = 0
        bytes = 0

        # Get BIN file and metadata
        bin_file, metadata = None, None
        for f in resource['local_paths']:
            # First check metadata attached to dataset in Clowder for item of interest
            if f.endswith('_dataset_metadata.json'):
                all_dsmd = terrautils.extractors.load_json_file(f)
                metadata = terrautils.metadata.get_extractor_metadata(all_dsmd)
            # Otherwise, check if metadata was uploaded as a .json file
            elif f.endswith('_metadata.json') and f.find('/_metadata.json') == -1 and metadata is None:
                metadata = terrautils.extractors.load_json_file(f)
            elif f.endswith('_ir.bin'):
                bin_file = f
        if None in [bin_file, metadata]:
            logging.error('could not find all both of ir.bin/metadata')
            return

        # Determine output directory
        png_path = terrautils.sensors.get_sensor_path_by_dataset("ua-mac", "Level_1", resource['dataset_info']['name'],
                                                                 "flir2tif", 'png')
        tiff_path = terrautils.sensors.get_sensor_path_by_dataset("ua-mac", "Level_1", resource['dataset_info']['name'],
                                                                  "flir2tif", 'tif')
        out_dir = os.path.dirname(png_path)
        logging.info("...writing outputs to: %s" % out_dir)
        if not os.path.exists(out_dir):
            os.makedirs(out_dir)
        uploaded_file_ids = []

        skipped_png = False
        if not os.path.exists(png_path) or self.force_overwrite:
            logging.info("...creating PNG image")
            # get raw data from bin file
            raw_data = numpy.fromfile(bin_file, numpy.dtype('<u2')).reshape([480, 640]).astype('float')
            raw_data = numpy.rot90(raw_data, 3)
            terrautils.extractors.create_image(raw_data, png_path, self.scale_values)
            # Only upload the newly generated file to Clowder if it isn't already in dataset
            if png_path not in resource["local_paths"]:
                fileid = pyclowder.files.upload_to_dataset(connector, host, secret_key, resource['id'], png_path)
                uploaded_file_ids.append(fileid)
            created += 1
            bytes += os.path.getsize(png_path)
        else:
            skipped_png = True

        if not os.path.exists(tiff_path) or self.force_overwrite:
            logging.info("...getting information from json file for geoTIFF")
            scan_time = terrautils.extractors.calculate_scan_time(metadata)
            gps_bounds = terrautils.extractors.calculate_gps_bounds(metadata, "flirIrCamera")
            if skipped_png:
                raw_data = numpy.fromfile(bin_file, numpy.dtype('<u2')).reshape([480, 640]).astype('float')
                raw_data = numpy.rot90(raw_data, 3)
            tc = getFlir.rawData_to_temperature(raw_data, scan_time, metadata) # get temperature

            logging.info("...creating TIFF image")
            # Rename temporary tif after creation to avoid long path errors
            out_tmp_tiff = "/home/extractor/"+resource['dataset_info']['name']+".tif"
            terrautils.extractors.create_geotiff(tc, gps_bounds, out_tmp_tiff, None, True)
            shutil.move(out_tmp_tiff, tiff_path)
            if tiff_path not in resource["local_paths"]:
                fileid = pyclowder.files.upload_to_dataset(connector, host, secret_key, resource['id'], tiff_path)
                uploaded_file_ids.append(fileid)
            created += 1
            bytes += os.path.getsize(tiff_path)

        # Tell Clowder this is completed so subsequent file updates don't daisy-chain
        metadata = terrautils.extractors.build_metadata(host, self.extractor_info['name'], resource['id'], {
            "files_created": uploaded_file_ids}, 'dataset')
        pyclowder.datasets.upload_metadata(connector, host, secret_key, resource['id'], metadata)

        endtime = datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
        terrautils.extractors.log_to_influxdb(self.extractor_info['name'], self.influx_params,
                                              starttime, endtime, created, bytes)

if __name__ == "__main__":
    extractor = FlirBin2JpgTiff()
    extractor.start()
