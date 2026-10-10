# Raspberry Pi LED test

This test turns an LED on for 2 seconds and off for 2 seconds repeatedly.

## Wiring

With the Raspberry Pi powered off, connect:

```text
GPIO 2 (physical pin 3) -> 330 ohm resistor -> LED long leg (+)
LED short leg (-)       -> GND (physical pin 6)
```

GPIO 2 is also the Pi's I2C data pin. If I2C is enabled or hardware is attached
to the I2C pins, use a general-purpose pin such as GPIO 17 instead and change
`LED_GPIO` in `blink_led.py`.

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

## Run it

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

## Start it automatically when the Pi boots

Create a system service:

```bash
sudo nano /etc/systemd/system/knighthacks-led.service
```

Paste the following, replacing `pi` with the Pi username in both paths if
needed:

```ini
[Unit]
Description=KnightHacks LED test
After=multi-user.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/pi_code
ExecStart=/usr/bin/python3 /home/pi/pi_code/blink_led.py
Restart=on-failure

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
