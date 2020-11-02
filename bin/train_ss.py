#!/usr/bin/env python

# Copyright 2020 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import yaml
import pprint
import argparse

from aps.utils import set_seed
from aps.opts import BaseTrainParser
from aps.trainer.ddp import DdpTrainer
from aps.loader import support_loader
from aps.transform import support_transform
from aps.task import support_task
from aps.conf import load_ss_conf
from aps.sse import support_nnet


def run(args):
    # set random seed
    seed = set_seed(args.seed)
    if seed is not None:
        print(f"Set random seed as {seed}")

    conf = load_ss_conf(args.conf)
    print("Arguments in yaml:\n{}".format(pprint.pformat(conf)), flush=True)

    data_conf = conf["data_conf"]
    trn_loader = support_loader(**data_conf["train"],
                                train=True,
                                fmt=data_conf["fmt"],
                                batch_size=args.batch_size,
                                num_workers=args.num_workers,
                                **data_conf["loader"])
    dev_loader = support_loader(**data_conf["valid"],
                                train=False,
                                fmt=data_conf["fmt"],
                                batch_size=args.batch_size,
                                num_workers=args.num_workers,
                                **data_conf["loader"])

    ss_cls = support_nnet(conf["nnet"])
    if "enh_transform" in conf:
        enh_transform = support_transform("enh")(**conf["enh_transform"])
        nnet = ss_cls(enh_transform=enh_transform, **conf["nnet_conf"])
    else:
        nnet = ss_cls(**conf["nnet_conf"])

    task = support_task(conf["task"], nnet, **conf["task_conf"])

    trainer = DdpTrainer(task,
                         device_ids=args.device_id,
                         checkpoint=args.checkpoint,
                         resume=args.resume,
                         init=args.init,
                         save_interval=args.save_interval,
                         prog_interval=args.prog_interval,
                         tensorboard=args.tensorboard,
                         **conf["trainer_conf"])

    # dump configurations
    conf["cmd_args"] = vars(args)
    with open(f"{args.checkpoint}/train.yaml", "w") as f:
        yaml.dump(conf, f)

    if args.eval_interval > 0:
        trainer.run_batch_per_epoch(trn_loader,
                                    dev_loader,
                                    num_epochs=args.epochs,
                                    eval_interval=args.eval_interval)
    else:
        trainer.run(trn_loader, dev_loader, num_epochs=args.epochs)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Command for speech separation/enhancement model training",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        parents=[BaseTrainParser.parser])
    parser.add_argument("--device-id",
                        type=str,
                        default="0",
                        help="Training on which GPU device")
    args = parser.parse_args()
    print("Arguments in args:\n{}".format(pprint.pformat(vars(args))),
          flush=True)
    run(args)
