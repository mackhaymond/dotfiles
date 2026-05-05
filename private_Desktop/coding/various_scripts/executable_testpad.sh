#!/bin/bash

if [[ $# -eq 0 ]]; then
    ssh root@10.0.0.100 'qm start 116'
elif [[ $1 -eq "start" ]]; then
    ssh root@10.0.0.100 'qm start 116'
elif [[ $1 -eq "stop" ]]; then
    ssh root@10.0.0.100 'qm shutdown 116'
fi
