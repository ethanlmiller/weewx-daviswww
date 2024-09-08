# DavisWWW

This is a `weewx` driver for web-based Davis Instrument weather reporters, including
WeatherLink Live and Airlink.

Features include:

- Reads observations from Davis Web-based reporters.
- Simple to configure: reasonable defaults.
- Can combine observations from weather station and air quality monitor into a single station.
- Can select which transmitter provides a given sensor for systems with multiple observation transmitters.

## Deploying

Place `daviswww.py` into the `bin/user` directory in your `weewx` data directory, which is usually
the one containing `weewx.conf`.

## Configuration section

This is a minimal configuration section, but contains most of the parameters that need to be configured.
At least one of `weather_host` and `aqi_host` needs to be specified, and may be either an IP address
or a host name. For most home installations, IP address is probably the right choice.

```
[DavisWWW]
    weather_host = 12.34.56.78
    aqi_host     = 12.34.56.89
    driver       = user.daviswww
```
