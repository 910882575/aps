# Copyright 2020 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)
"""
Load {am,lm,ss} training configurations
"""
import yaml
import codecs

from typing import Dict, List, Tuple

base_keys = [
    "nnet", "nnet_conf", "task", "task_conf", "data_conf", "trainer_conf"
]
required_ss_conf_keys = base_keys + ["enh_transform"]
required_am_conf_keys = base_keys + ["enh_transform", "asr_transform"]
required_lm_conf_keys = base_keys


def load_dict(dict_path: str) -> Dict:
    """
    Load the dictionary object
    """
    with codecs.open(dict_path, encoding="utf-8") as f:
        vocab = {}
        for line in f:
            unit, idx = line.split()
            vocab[unit] = int(idx)

    if "<sos>" not in vocab or "<eos>" not in vocab:
        raise ValueError(f"Missing <sos>/<eos> in {dict_path}")
    return vocab


def check_conf(conf: Dict, constrained: List[str]) -> Dict:
    """
    Check the format of the configurations
    """
    # check the invalid item
    for key in conf.keys():
        if key not in constrained + ["cmd_args"]:
            raise ValueError(f"Get invalid configuration item: {key}")
    # create task_conf if None
    if "task_conf" not in conf:
        conf["task_conf"] = {}
    # check the missing items
    for key in constrained:
        if key not in conf.keys():
            raise ValueError(f"Miss the item in the configuration: {key}?")
    return conf


def load_ss_conf(yaml_conf: str) -> Dict:
    """
    Load yaml configurations for speech separation/enhancement tasks
    """
    with open(yaml_conf, "r") as f:
        conf = yaml.full_load(f)
    return check_conf(conf, required_ss_conf_keys)


def load_lm_conf(yaml_conf: str, dict_path: str) -> Dict:
    """
    Load yaml configurations for language model training
    """
    with open(yaml_conf, "r") as f:
        conf = yaml.full_load(f)
    conf = check_conf(conf, required_lm_conf_keys)
    vocab = load_dict(dict_path)
    return conf, vocab


def load_am_conf(yaml_conf: str, dict_path: str) -> Tuple[Dict, Dict]:
    """
    Load yaml configurations for acoustic model training
    """
    with open(yaml_conf, "r") as f:
        conf = yaml.full_load(f)
    conf = check_conf(conf, required_am_conf_keys)

    # add dict info
    nnet_conf = conf["nnet_conf"]
    vocab = load_dict(dict_path)
    nnet_conf["vocab_size"] = len(vocab)

    task_conf = conf["task_conf"]
    use_ctc = "ctc_weight" in task_conf and task_conf["ctc_weight"] > 0
    is_transducer = conf["task"] == "transducer"
    if not is_transducer:
        nnet_conf["sos"] = vocab["<sos>"]
        nnet_conf["eos"] = vocab["<eos>"]
    # for CTC/RNNT
    if use_ctc or is_transducer:
        conf["task_conf"]["blank"] = len(vocab)
        # add blank
        nnet_conf["vocab_size"] += 1
        if is_transducer:
            nnet_conf["blank"] = len(vocab)
        else:
            nnet_conf["ctc"] = use_ctc
    return conf, vocab
