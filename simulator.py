"""Entry point for the simulated biodigester hardware."""
import argparse

import config
from mqtt_common import connect_and_start, make_client
from simulation.simulator_service import SimulatorService


def main():
    """Read command-line settings and start the simulated hardware."""
    parser = argparse.ArgumentParser(description="Simulated biodigester hardware")
    parser.add_argument("--speed", type=float, default=config.SIM_SPEED)
    parser.add_argument("--start", type=float, default=config.SIM_START_C)
    args = parser.parse_args()

    client = make_client("biodigester-simulator")
    simulator = SimulatorService(client, start_c=args.start, speed=args.speed)
    connect_and_start(client, "simulator")
    try:
        simulator.run()
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
