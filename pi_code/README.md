# Raspberry Pi LED control

The blink test verifies the wiring. The WebSocket server then lets the gaming PC
turn the LED on while its reticle is aligned with a detected head.

## Wiring

With the Raspberry Pi powered off, connect:

```text
GPIO 2 (physical pin 3) -> 330 ohm resistor -> LED long leg (+)
LED short leg (-)       -> GND (physical pin 6)
```

GPIO 2 is also the Pi's I2C data pin. If I2C is enabled or hardware is attached
to the I2C pins, use a general-purpose pin such as GPIO 17 instead and change
`LED_GPIO` in `blink_led.py`. Start the WebSocket server with `--gpio 17` when
using that alternate pin.

## Copy it to the Pi

On the PC, open a terminal in the repository and run (replace the hostname or
username if needed):

```bash
scp -r pi_code pi@raspberrypi.local:~/
```

Then connect to the Pi:

```bash
ssh pi@raspberrypi.local
```

## Run the blink test

Raspberry Pi OS normally includes gpiozero. If it is missing, install it with:

```bash
sudo apt update
sudo apt install -y python3-gpiozero
```

Start the test:

```bash
python3 ~/pi_code/blink_led.py
```

Press Ctrl+C to stop. The GPIO is turned off when the program exits.

## Install the WebSocket dependency

Raspberry Pi OS may prevent system-wide `pip` installs, so create a virtual
environment that can also use the system-installed gpiozero package:

```bash
sudo apt update
sudo apt install -y python3-gpiozero python3-venv
python3 -m venv --system-site-packages ~/pi_code/.venv
~/pi_code/.venv/bin/pip install -r ~/pi_code/requirements.txt
```

## Start the LED WebSocket server

```bash
~/pi_code/.venv/bin/python ~/pi_code/led_websocket_server.py
```

It listens on port `8765`. Leave it running, then start the alignment overlay on
the gaming PC. The LED turns off if the PC disconnects or the server receives no
command for one second.

If `raspberrypi.local` does not work from the PC, get the Pi's IP address with:

```bash
hostname -I
```

Then run `python valorant_alignment_overlay.py --pi-host PI_ADDRESS` on the PC.
The overlay should display `PI CONNECTED` once the connection succeeds.

## Start the WebSocket server automatically when the Pi boots

Create a system service:

```bash
sudo nano /etc/systemd/system/knighthacks-led.service
```

Paste the following, replacing `pi` with the Pi username in both paths if
needed:

```ini
[Unit]
Description=KnightHacks LED WebSocket server
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pi_code
ExecStart=/home/pi/pi_code/.venv/bin/python /home/pi/pi_code/led_websocket_server.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
```

Save it, then enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now knighthacks-led.service
```

Check its status with:

```bash
systemctl status knighthacks-led.service
```

## Bench-test the first stepper motor

The first TMC2209 uses these BCM GPIO connections:

```text
GPIO 17 (physical pin 11) -> STEP
GPIO 27 (physical pin 13) -> DIR
GPIO 22 (physical pin 15) -> EN
```

With MS1 and MS2 disconnected, the TMC2209's internal pull-down resistors select
1/8 microstepping. Before testing, verify all wiring with power disconnected,
install the driver heatsink, and connect the motor before applying 12V. Never
connect or disconnect a motor while the driver is powered.

Stop the LED WebSocket server if it is running, then run:

```bash
python3 ~/knighthacks/pi_code/test_stepper.py
```

The default test moves the motor one-quarter revolution at about 5 RPM, pauses,
returns it to its starting position, and disables the driver. Press Ctrl+C at
any time to stop and disable it. To request a different small test movement:

```bash
python3 ~/knighthacks/pi_code/test_stepper.py --revolutions 0.5 --rpm 8
```
