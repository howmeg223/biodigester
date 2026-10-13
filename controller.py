"""Entry point for the biodigester controller process."""
import argparse
import signal
import time

import config
from controller_system.biodigester_controller import BiodigesterController
from mqtt_common import connect_and_start, make_client


def stop_requested(signum, frame):
    raise KeyboardInterrupt


def main():
    """Connect to MQTT and run one control cycle at a time until stopped."""
    parser = argparse.ArgumentParser(description="Biodigester controller")
    parser.add_argument("--strategy", choices=["hysteresis", "simple"], default=config.STRATEGY)
    args = parser.parse_args()

    client = make_client("biodigester-controller")
    controller = BiodigesterController(client, strategy=args.strategy)
    signal.signal(signal.SIGTERM, stop_requested)
    connect_and_start(client, "controller")

    print(f"[controller] Strategy: {args.strategy}. Local safety decisions remain active without the dashboard.")
    try:
        while True:
            controller.step()
            time.sleep(config.CONTROL_PERIOD_S)
    except KeyboardInterrupt:
        pass
    finally:
        controller.shutdown()
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
