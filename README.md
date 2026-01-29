# LaundryIQ Data Logger

BLE current sensor data logging application for LaundryIQ devices. Logs current sensor data (CT clamp, Hall1, Hall2), displays real-time plots, tracks machine states, and exports to CSV.

## Features

- **BLE Connection**: Connects to LaundryIQ devices via Bluetooth Low Energy (Nordic UART Service)
- **Real-time Plotting**: Live visualization of current sensor data with hover tooltips
- **Multi-sensor Support**: CT clamp and dual Hall effect sensors
- **State Tracking**: Manual state annotation (On, Off, Wash, Rinse, Spin, Dry, etc.)
- **Calibration**: Built-in calibration calculator for each sensor
- **CSV Export**: Automatic logging with timestamped filenames based on machine settings

## Requirements

### Hardware
- LaundryIQ BLE device with current sensing firmware
- Computer with Bluetooth capability

### Software
- Python 3.8+
- Windows/macOS/Linux

## Installation

```bash
# Clone the repository
git clone https://github.com/TechRider5/LIQ_Current_Sensing_DataLogging.git
cd LIQ_Current_Sensing_DataLogging

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
python laundryiq_logger.py
```

### Quick Start

1. **Scan**: Enter device name filter (default: "LIQ") and click Scan
2. **Connect**: Select your device from the list and click Connect
3. **Configure**: Set machine type (Washer/Dryer) and cycle settings
4. **Log**: Click Start to begin logging, use state buttons to annotate machine phases
5. **Export**: Click Stop when done, data auto-saves to Downloads folder

### Calibration

To calibrate sensors:
1. Measure actual current with a multimeter
2. Enter the multimeter reading (MM) and sensor reading (Sen)
3. Click Calc to compute the calibration factor

## Data Format

CSV output includes:
| Column | Description |
|--------|-------------|
| timestamp | ISO format datetime |
| ct_clamp | CT clamp current (A) |
| hall1 | Hall sensor 1 current (A) |
| hall2 | Hall sensor 2 current (A) |
| state | Machine state annotation |
| elapsed_time | Seconds since logging started |

## File Naming

Files are automatically named based on settings:
- Washer: `YYYYMMDD_HHMMSS_Washer_Cycle_Temp_Spin.csv`
- Dryer: `YYYYMMDD_HHMMSS_Dryer_DryLevel_Temp_Time.csv`

## License

MIT License - See LICENSE file for details.
