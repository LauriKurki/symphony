import argparse
import os
import subprocess

# import time
import sys

sys.path.append("..")

import qm9  # noqa: E402


def main(chunk: int = 2976, num_seeds: int = 8, root_dir: str = "data"):
    qm9_data = qm9.load_qm9("qm9_data")
    processes = []

    for seed in range(num_seeds):
        for start in range(0, len(qm9_data), chunk):
            end = start + chunk
            path = f"{root_dir}/fragments_{seed:02d}_{start:06d}_{end:06d}"

            if os.path.exists(path):
                print(f"Skip {path}")
                continue

            print(f"Dispatching {path}")

            # run non-blocking
            p = subprocess.Popen(
                [
                    # "srun",
                    # "--mem=4G",
                    # "--ntasks=1",
                    # "--cpus-per-task=8",
                    # "--gres=gpu:1",
                    "python",
                    "fragmenter.py",
                    "--seed",
                    str(seed),
                    "--start",
                    str(start),
                    "--end",
                    str(end),
                    "--output",
                    path,
                ]
            )
            processes.append(p)

            # wait a bit to avoid overloading the scheduler
            # time.sleep(10.0)

            # actually wait for the process to finish
            p.wait()

    print("Waiting for processes to finish...")
    for p in processes:
        p.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunk", type=int, default=2976)
    parser.add_argument("--num_seeds", type=int, default=8)
    parser.add_argument("--root_dir", type=str, default="data")
    args = parser.parse_args()
    main(**vars(args))
