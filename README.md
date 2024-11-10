## hcalory-control

A little tool to control BLE-capable Hcalory heaters. This has only been tested on the Hcalory W1 model.

### CLI Usage

```
❯ hcalory-control --help
usage: hcalory-control [-h] --address ADDRESS {start_heat,stop_heat,up,down,gear,thermostat,pump_data}

positional arguments:
  {start_heat,stop_heat,up,down,gear,thermostat,pump_data}

options:
  -h, --help            show this help message and exit
  --address ADDRESS     Bluetooth MAC address of heater
```

To get the current state of the heater, use `pump_data`:
```
❯ hcalory-control --address ec:b1:c3:00:4d:61 pump_data
{
    "ambient_temperature": 87,
    "body_temperature": 226,
    "heater_mode": "thermostat",
    "heater_setting": 74,
    "heater_state": "running",
    "voltage": 13
}
```

Temperature/"gear" mode changes are done by incrementing/decrementing, not by setting the absolute value you want:
```
❯ hcalory-control --address ec:b1:c3:00:4d:61 up
Before command:
{
    "ambient_temperature": 86,
    "body_temperature": 219,
    "heater_mode": "thermostat",
    "heater_setting": 74,
    "heater_state": "running",
    "voltage": 13
}
After command:
{
    "ambient_temperature": 86,
    "body_temperature": 219,
    "heater_mode": "thermostat",
    "heater_setting": 75,
    "heater_state": "running",
    "voltage": 13
}
```

### Usage as a library
The `main` and `run_command` functions should serve as a decent example. Getting the current running state out of these heaters is _weird_. In order to get your notification on the read characteristic to give you anything, you have to ask the heater to send something by writing to the write characteristic. The official app does this once a second, which seems _wildly_ inefficient to me. I've no idea why they didn't just make the heater push data out on its own when things changed.
