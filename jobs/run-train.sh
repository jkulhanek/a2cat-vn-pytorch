#!/bin/bash
cat ~/experiments/target-driven-visual-navigation/jobs/job-template.sh | sed "s/{jobname}/$1/g" | '__job__.sh'
sbatch '__job__.sh'
rm '__job__.sh'