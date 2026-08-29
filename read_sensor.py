import argparse
import time
from collections import deque

import serial


def calculate_checksum(data_h, data_l):
    return (0xFF + data_h + data_l) & 0xFF


def parse_distance_mm(data_h, data_l):
    return (data_h << 8) + data_l


def is_valid_reading(distance_mm):
    # Filter out invalid dead-zone readings (usually 0 or out of bounds)
    return distance_mm > 30


def read_frame(ser):
    """Try to read one A02YYUW frame. Returns distance_mm, or None if no
    valid frame was available this call."""
    # Check if we have enough bytes in the buffer to make a full 4-byte frame,
    # and read a single byte looking for the 0xFF header
    if ser.in_waiting >= 4 and ser.read(1) == b'\xff':
        # Grab the remaining 3 bytes of this frame immediately
        data = ser.read(3)

        if len(data) == 3:
            data_h, data_l, checksum = data[0], data[1], data[2]

            if calculate_checksum(data_h, data_l) == checksum:
                return parse_distance_mm(data_h, data_l)

            # If checksum fails, we likely misaligned; flush buffer to reset
            ser.reset_input_buffer()

    return None


def run(ser, window_size):
    # Rolling window of the last N valid readings
    readings = deque(maxlen=window_size)

    print(f"Listening for A02YYUW Automatic Stream (window size: {window_size})... Press Ctrl+C to stop.")

    try:
        while True:
            distance_mm = read_frame(ser)

            if distance_mm is not None:
                if is_valid_reading(distance_mm):
                    readings.append(distance_mm)
                    avg_mm = sum(readings) / len(readings)
                    avg_cm = avg_mm / 10.0

                    print(f"Distance: {avg_cm:.1f} cm  ({avg_mm:.0f} mm avg over {len(readings)} readings)")
                else:
                    print("Reading: Too close (Below 3cm blind zone)")

            # Tiny sleep to avoid pegging the CPU
            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\nStopping streaming reader.")
    finally:
        ser.close()


def main():
    parser = argparse.ArgumentParser(description="A02YYUW distance reader with rolling average smoothing")
    parser.add_argument("--window-size", type=int, default=25, help="number of readings to average over (default: 25)")
    args = parser.parse_args()

    # Initialize serial port at 9600 baud rate
    ser = serial.Serial('/dev/serial0', baudrate=9600, timeout=1)
    run(ser, args.window_size)


if __name__ == "__main__":
    main()
