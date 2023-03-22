#!/bin/bash

for l in 0 1 2 3
do
  for c in 32
  do
    for i in 1 2 3 4
    do
        grun python -m main --config=configs/mace.py     --config.max_ell="$l" --config.num_channels="$c" --config.num_interactions="$i" --workdir=workdirs/v3/mace/interactions="$i"/l="$l"/channels="$c"/     2> workdirs/v3/out.txt &
        sleep 10
        grun python -m main --config=configs/e3schnet.py --config.max_ell="$l" --config.num_channels="$c" --config.num_interactions="$i" --workdir=workdirs/v3/e3schnet/interactions="$i"/l="$l"/channels="$c"/ 2> workdirs/v3/out.txt &
        sleep 10
        grun python -m main --config=configs/nequip.py   --config.max_ell="$l" --config.num_channels="$c" --config.num_interactions="$i" --workdir=workdirs/v3/nequip/interactions="$i"/l="$l"/channels="$c"/   2> workdirs/v3/out.txt &
        sleep 10
    done
  done
done

