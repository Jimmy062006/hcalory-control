## hcalory-control

A little tool to control BLE-capable Hcalory heaters. This has only been tested on the Hcalory W1 model.

### Usage

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
❯ hcalory-control --address ec:b1:c3:00:4d:61  pump_data
{
    "ambient_temperature": 87,
    "body_temperature": 226,
    "heater_mode": "thermostat",
    "heater_setting": 74,
    "heater_state": "running",
    "voltage": 13
}
```
