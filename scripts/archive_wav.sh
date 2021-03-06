#!/usr/bin/env bash

# Copyright 2020 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

# Archive wav.scp to wav.ark

set -eu

nj=32
max_jobs_run=""
cmd="utils/run.pl"

echo "$0 $*"

. ./utils/parse_options.sh || exit 1

[ $# -ne 2 ] && echo "format error: $0 <data-dir> <ark-dir>" && exit 1

data_dir=$(cd $1; pwd)
ark_dir=$2

[ ! -f $data_dir/wav.scp ] && echo "$0: Missing wav.scp in $data_dir" && exit

mkdir -p $ark_dir && ark_dir=$(cd $ark_dir; pwd)

split_id=$(seq $nj)
mkdir -p $data_dir/split$nj

split_wav_scp=""
for n in $split_id; do split_wav_scp="$split_wav_scp $data_dir/split$nj/wav.$n.scp"; done

./utils/split_scp.pl $data_dir/wav.scp $split_wav_scp

[ ! -z $max_jobs_run ] && cmd="$cmd --max-jobs-run $max_jobs_run"

exp=$(basename $data_dir)
$cmd JOB=1:$nj exp/archive_wav/$exp/archive_wav.JOB.log \
  utils/archive_wav.py --scp $ark_dir/wav.JOB.scp \
  $data_dir/split$nj/wav.JOB.scp $ark_dir/wav.JOB.ark

echo "$0: Archive wav.scp from $data_dir to $ark_dir done"
