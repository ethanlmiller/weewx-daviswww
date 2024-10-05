#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Davis WWW driver for WeeWX
#
# Copyright 2024 Ethan L. Miller
#
# Based on wll.py, Copyright 2020 Jon Otaegi
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.
#
# See http://www.gnu.org/licenses/

""" WeeWX driver for Davis WWW-accessible devices

The Davis WeatherLink Live (WLL) has an HTTP interface that returns current weather
data in JSON format, described at https://weatherlink.github.io/weatherlink-live-local-api/.
It can also query an Airlink Live device for air quality information.
This driver polls the interface and creates a packet with current weather conditions,
and returns it to WeeWX.

The driver supports a WLL that reports information for multiple sensors of the same
type; weewx.conf can specify which sensors are reported for each field.

The driver can be used for WLL and/or Airlink; it combines the observations into a single
record.

Requires:
* Python3
* requests module (install with apt/yum/pkg or pip)

++++++ IMPORTANT: this driver is *only* compatible with Python3. ++++++

"""

import time
import requests
import logging
from collections import namedtuple
import unittest
from pprint import pprint

import weewx.drivers

DRIVER_NAME = 'DavisWWW'
DRIVER_VERSION = '0.1'

MM_TO_INCH = 0.0393701
rain_collector_scale = {
    1: 0.01,
    2: 0.2 * MM_TO_INCH,
    3: 0.1,
    4: 0.001 * MM_TO_INCH,
}

log = logging.getLogger(__name__)

def track_total_rain (drvr, data, annual_rain):
    """
    Return the amount of rain that's fallen since the last call to this function.
    If this is the first call to the function, return zero and set annual rain to current annual rain total.
    If the annual rain total decreased, we must have wrapped around a year, so set the "last rainfall" to 0.
    Track total rain amount in absolute units.
    """
    # Scale rain *first* so all stored amounts are in absolute units
    total_rain_scaled = scale_rain (drvr, data, annual_rain)
    if drvr.total_rain_scaled == None:
        # First time we've run the driver, so start with the current total
        drvr.total_rain_scaled = total_rain_scaled
    elif total_rain_scaled < drvr.total_rain_scaled:
        # New month, so reset total rain to this point
        drvr.total_rain_scaled = 0
    # Amount that's fallen is the difference between the last reading and this reading
    rain_amt = total_rain_scaled - drvr.total_rain_scaled
    drvr.total_rain_scaled = total_rain_scaled
    return rain_amt

def scale_rain (drvr, data, amt):
    """
    Scale rainfall by the current scale.
    """
    return amt * drvr.rain_scale_factor

# Table of sensor information.
# Includes Davis WLL name (from JSON), default scale, sensor "group" (for mappings), sensor type, and function to generate value (if any).
SensorInfo = namedtuple ('SensorInfo', ['wllname', 'factor', 'metric_type', 'txid_group', 'function'])

def loader(config_dict, engine):
    return DavisWWW(**config_dict['DavisWWW'])

class DavisWWW(weewx.drivers.AbstractDevice):
    @property
    def default_stanza(self):
        return """
[DavisWWW]
    # This section is for Davis Web-based reporters.

    #---------------------------------
    # REQUIRED specifications
    #---------------------------------
    
    # The hostname or ip address of the Davis devices on the local network.
    # At least one of weather_host and aqi_host must be specified.
    # If both are specified, the station will report both weather and air quality in the same record.
    # For the driver to work, the Davis device(s) and the computer running Weewx should be on the same network.
    # For details on programmatically finding Davis devices on the local network,
    # see https://weatherlink.github.io/weatherlink-live-local-api/discovery.html
    weather_host = 10.0.0.100
    aqi_host = 10.0.0.101

    # The driver to use:
    driver = user.daviswww

    #---------------------------------
    # OPTIONAL specifications
    #
    # The defaults for these specifications are usually fine.
    # They may be modified if necessary
    #---------------------------------
    
    # How often to poll the weather data (in seconds).
    # The interface can support continuous requests as often as every 10 seconds.
    # Default: 60 seconds
    poll_interval = 60

    # The default weather transmitter ID (1-8).
    # If this is omitted, weewx will use the weather readings from the lowest-numbered transmitter.
    weather_transmitter_id = 1

    # Default soil sensor transmitter ID (1-8):
    # If this is omitted, weewx will use the soil readings from the lowest-numbered transmitter. 
    soil_transmitter_id = 2

    # Wind measurement averaging: number of minutes
    # Davis supports three (more, but we only support three) types of wind measurement:
    # 0: most recent reading
    # 1: average over the past minute
    # 2: average over the past 2 minutes
    wind_measurement: 1

    # Rain collector type
    # This is a feature of the Davis rain collector. It's unlikely that you'll need to
    # specify a different rain collector, but the option is available.
    # The rain collector types are:
    # 1: 0.01 inches per tip (most common, default)
    # 2: 0.02 mm per tip
    # 3: 0.1 inches per tip
    # 4: 0.001 mm per tip
    rain_collector: 1

    # More detailed mappings of sensors to transmitter IDs.
    # Only specify cases where the default transmitter ID isn't correct.
    # Mappings are for temp (includes humidity), wind, rain, uv, solar, and battery.
    # Mappings are also for soil temp (soil1, soil2, soil3, soil4) and soil moisture (moist1, moist2, moist3, moist4)
    # If no transmitter is specified for a measurement (either globally as above, or locally in mappings),
    # sensors are sampled in the order from transmitters_ordered
    mappings = outTemp:A, windSpeed:5, soil1:2, soil2:2 moist1:2

    # Default transmitter ordering (12345678AIB) is usually fine,
    # so there's no need to specify unless you want a different default ordering.
    # A = air quality
    # B = barometer (measured from indoor, typically)
    # I = indoor unit
    transmitters_ordered = A12345678IB

"""

    def __init__(self, **stn_dict):
        # NOTE:
        #
        # We're using the wind averages for the last 1 minute. This seems more reasonable than the
        # instantaneous averages.
        # But if you want those instantaneous averages, 
        self.sensor_info = {
            'outTemp'           : SensorInfo ('temp', 1, 'temp', 'W', None),
            'outHumidity'       : SensorInfo ('hum', 1, 'temp', 'W', None),
            'dewpoint'          : SensorInfo ('dew_point', 1, 'temp', 'W', None),
            'heatindex'         : SensorInfo ('heat_index', 1, 'temp', 'W', None),
            'THSW'              : SensorInfo ('thsw_index', 1, 'temp', 'W', None),
            'windchill'         : SensorInfo ('wind_chill', 1, 'wind', 'W', None),
            'windSpeed'         : SensorInfo ('wind_speed_avg_last_1_min', 1, 'wind', 'W', None),
            'windDir'           : SensorInfo ('wind_dir_scalar_avg_last_1_min', 1, 'wind', 'W', None),
            'windGust'          : SensorInfo ('wind_speed_hi_last_2_min', 1, 'wind', 'W', None),
            'windGustDir'       : SensorInfo ('wind_dir_at_hi_speed_last_2_min', 1, 'wind', 'W', None),
            'rain'              : SensorInfo ('rainfall_monthly', 1, 'rain', 'W', track_total_rain),
            'rainRate'          : SensorInfo ('rain_rate_last', 1, 'rain', 'W', scale_rain),
            'radiation'         : SensorInfo ('solar_rad', 1, 'solar', 'W', None),
            'UV'                : SensorInfo ('uv_index', 1, 'uv', 'W', None),
            'txBatteryStatus'   : SensorInfo ('trans_battery_flag', 1, 'battery', 'W', None),
            'soilTemp1'         : SensorInfo ('temp_1', 1, 'soil1', 'S', None),
            'soilTemp2'         : SensorInfo ('temp_2', 1, 'soil2', 'S', None),
            'soilTemp3'         : SensorInfo ('temp_3', 1, 'soil3', 'S', None),
            'soilTemp4'         : SensorInfo ('temp_4', 1, 'soil4', 'S', None),
            'soilMoist1'        : SensorInfo ('moist_soil_1', 1, 'moist1', 'S', None),
            'soilMoist2'        : SensorInfo ('moist_soil_2', 1, 'moist2', 'S', None),
            'soilMoist3'        : SensorInfo ('moist_soil_3', 1, 'moist3', 'S', None),
            'soilMoist4'        : SensorInfo ('moist_soil_4', 1, 'moist4', 'S', None),
            'barometer'         : SensorInfo ('bar_sea_level', 1, 'bar', 'B', None),
            'pressure'          : SensorInfo ('bar_absolute', 1, 'bar', 'B', None),
            'inTemp'            : SensorInfo ('temp_in', 1, 'indoor', 'I', None),
            'inHumidity'        : SensorInfo ('hum_in', 1, 'indoor', 'I', None),
            'inDewpoint'        : SensorInfo ('dew_point_in', 1, 'indoor', 'I', None),
            'pm1_0'             : SensorInfo ('pm_1', 1, 'aqi', 'A', None),
            'pm2_5'             : SensorInfo ('pm_2p5', 1, 'aqi', 'A', None),
            'pm10_0'            : SensorInfo ('pm_10', 1, 'aqi', 'A', None),
        }
        self.wind_measurement = stn_dict.get('wind_measurement', 1)
        if self.wind_measurement == 0:
            self.sensor_info['windSpeed'] = SensorInfo ('wind_speed_last', 1, 'wind', 'W', None)
            self.sensor_info['windDir']   = SensorInfo ('wind_dir_last', 1, 'wind', 'W', None)
        elif self.wind_measurement == 1:
            self.sensor_info['windSpeed'] = SensorInfo ('wind_speed_avg_last_1_min', 1, 'wind', 'W', None)
            self.sensor_info['windDir']   = SensorInfo ('wind_dir_scalar_avg_last_1_min', 1, 'wind', 'W', None)
        elif self.wind_measurement == 2:
            self.sensor_info['windSpeed'] = SensorInfo ('wind_speed_avg_last_2_min', 1, 'wind', 'W', None),
            self.sensor_info['windDir']   = SensorInfo ('wind_dir_scalar_avg_last_2_min', 1, 'wind', 'W', None)
        self.hardware = stn_dict.get('hardware', 'DavisWWW')
        self.weather_host = stn_dict.get('weather_host', None)
        if self.weather_host:
            self.weather_url = "http://{0}:80/v1/current_conditions".format (self.weather_host)
        self.aqi_host = stn_dict.get('aqi_host', None)
        if self.aqi_host:
            self.aqi_url = "http://{0}:80/v1/current_conditions".format (self.aqi_host)
        if (not self.weather_url) and (not self.aqi_url):
            log.error("Must specify weather_host and/or aqi_host!")
        self.poll_interval = float(stn_dict.get('poll_interval', 60))
        if not 5 <= self.poll_interval <= 600:
            log.error("Invalid poll_interval {0} (10 <= poll_interval <= 600) - using default of 60.".format (self.poll_interval))
            self.poll_interval = 60
        # Set total rain to None so it initializes properly
        self.total_rain_scaled = None
        # Default is rain collector type 1
        self.rain_scale_type = int(stn_dict.get ('rain_collector', 1))
        if self.rain_scale_type not in (1,2,3,4):
            log.error("Invalid rain_collector {0} - defaulting to 1.".format (self.rain_scale_type))
            self.rain_scale_type = 1
        self.rain_scale_factor = self.get_rain_scale_factor (self.rain_scale_type)
        self.all_txids = str(stn_dict.get ('transmitters_ordered',
                                           '12345678BIA'))
        self.txids = dict()
        self.default_weather_txid = str(stn_dict.get ('weather_transmitter_id', 1))
        self.default_soil_txid = str(stn_dict.get ('soil_transmitter_id', 2))

        self.mappings = stn_dict.get ('mappings')
        self.init_txids (self.mappings)

    def hardware_name(self):
        return self.hardware

    def init_txids (self, mappings):
        # Initialize default txids by large-scale group
        default_txids = {'W': self.default_weather_txid, 'S': self.default_soil_txid, 'B': 'B', 'I': 'I', 'A': 'A'}
        for c in self.sensor_info.values():
            self.txids[c.wllname] = default_txids[c.txid_group]
        # Set up different txids for individual mappings
        if mappings:
            for m in mappings.split ():
                try:
                    (metric_type, txid) = m.split (':')
                    if metric_type in self.sensor_info:
                        self.txids[self.sensor_info[metric_type].wllname] = str(txid)
                except:
                    pass

    def get_rain_scale_factor (self, collector_type):
        return rain_collector_scale.get (collector_type, None)

    def get_condition (self, data, condition):
        # If we specified a sensor and it's available, use it
        if (self.txids[condition], condition) in data:
            return data[self.txids[condition],condition]
        # Otherwise, pick the first value we find
        for tx in self.all_txids:
            if (tx,condition) in data:
                return data[tx,condition]
        return None

    def parse_into_data (self, json_data, data):
        # Store JSON data into a normal dictionary for later processing
        # This allows us to pick the "best" transmitter ID
        for c in json_data['conditions']:
            record_type = c['data_structure_type']
            if record_type in (1,2):
                txid = str(c['txid'])
            elif record_type == 3:
                txid = 'B'
            elif record_type == 4:
                txid = 'I'
            elif record_type == 6:
                txid = 'A'
            for k,v in c.items():
                data[txid,k] = v

    def genLoopPackets (self):
        while True:
            # Create packet with reasonable defaults
            pkt = {
                'dateTime': time.time(),
                'usUnits': weewx.US,
            }
            station_data = dict()

            # Do AQI first, then weather. This ensures that dateTime is set from weather if both
            # aqi_host and weather_host are specified            
            try:
                if self.aqi_host:
                    response = requests.get(self.aqi_url, timeout=4)
                    response.raise_for_status()
                    json_data = response.json()['data']
                    pkt['dateTime'] = json_data['ts']
                    self.parse_into_data (json_data, station_data)
            except requests.exceptions.Timeout as e:
                log.error("Timeout getting air quality:", repr(e))
            except Exception as e:
                log.error (repr(e))

            try:
                if self.weather_host:
                    response = requests.get(self.weather_url, timeout=4)
                    response.raise_for_status()
                    json_data = response.json()['data']
                    pkt['dateTime'] = json_data['ts']
                    self.parse_into_data (json_data, station_data)
            except requests.exceptions.Timeout as e:
                log.error("Timeout getting weather:", repr(e))
            except Exception as e:
                log.error (repr(e))

            for (metric, info) in self.sensor_info.items():
                value = self.get_condition (station_data, info.wllname)
                if value != None:
                    value *= info.factor
                    if info.function:
                        value = info.function(self, station_data, value)
                    pkt.update ({metric: value})
            yield pkt
            time.sleep(self.poll_interval)

if __name__ == '__main__':
    import weeutil.weeutil
    import weeutil.logger
    import weewx

    weeutil.logger.setup ("DavisWWW")
    weewx.debug = 1
    driver = DavisWWW(weather_host="192.168.22.150", aqi_host="192.168.22.151",
                      weather_transmitter_id=5,
                      mappings="outTemp:A",
                      poll_interval=5)
    pprint (vars(driver))
    for packet in driver.genLoopPackets():
        print(weeutil.weeutil.timestamp_to_string(packet['dateTime']), packet)

