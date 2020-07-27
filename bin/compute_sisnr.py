#!/usr/bin/env python

# wujian@2018

import argparse

from tqdm import tqdm

from collections import defaultdict
from aps.metric.snr import si_snr, permute_si_snr
from aps.loader.am import WaveReader

from kaldi_python_io import Reader as BaseReader


class SpeakersReader(object):
    def __init__(self, scps, sr=16000):
        split_scps = scps.split(",")
        if len(split_scps) == 1:
            raise RuntimeError("Construct SpeakersReader need more "
                               "than one script, got {}".format(scps))
        self.readers = [WaveReader(scp, sr=sr) for scp in split_scps]

    def __len__(self):
        first_reader = self.readers[0]
        return len(first_reader)

    def __getitem__(self, key):
        data = []
        for reader in self.readers:
            wave = reader[key]
            data.append(wave if wave.ndim == 1 else wave[0])
        return data

    def __iter__(self):
        first_reader = self.readers[0]
        for key in first_reader.index_keys:
            yield key, self[key]


class Report(object):
    def __init__(self, spk2class=None):
        self.s2c = BaseReader(spk2class) if spk2class else None
        self.snr = defaultdict(float)
        self.cnt = defaultdict(int)

    def add(self, key, val):
        cls_str = "NG"
        if self.s2c:
            cls_str = self.s2c[key]
        self.snr[cls_str] += val
        self.cnt[cls_str] += 1

    def report(self):
        print("SI-SDR(dB) Report: ")
        tot_utt = sum([self.cnt[cls_str] for cls_str in self.cnt])
        tot_snr = sum([self.snr[cls_str] for cls_str in self.snr])
        print("Total: {:d}/{:.3f}".format(tot_utt, tot_snr / tot_utt))
        if len(self.snr) != 1:
            for cls_str in self.snr:
                cls_snr = self.snr[cls_str]
                num_utt = self.cnt[cls_str]
                print("\t{}: {:d}/{:.3f}".format(cls_str, num_utt,
                                                 cls_snr / num_utt))


def run(args):
    single_speaker = len(args.sep_scp.split(",")) == 1
    reporter = Report(args.spk2class)
    details = open(args.details, "w") if args.details else None

    if single_speaker:
        sep_reader = WaveReader(args.sep_scp, sr=args.sr)
        ref_reader = WaveReader(args.ref_scp, sr=args.sr)
        for key, sep in tqdm(sep_reader):
            ref = ref_reader[key]
            if sep.size != ref.size:
                end = min(sep.size, ref.size)
                sep = sep[:end]
                ref = ref[:end]
            snr = si_snr(sep, ref)
            reporter.add(key, snr)
            if details:
                details.write("{}\t{:.2f}\n".format(key, snr))
    else:
        sep_reader = SpeakersReader(args.sep_scp, sr=args.sr)
        ref_reader = SpeakersReader(args.ref_scp, sr=args.sr)
        for key, sep_list in tqdm(sep_reader):
            ref_list = ref_reader[key]
            if sep_list[0].size != ref_list[0].size:
                end = min(sep_list[0].size, ref_list[0].size)
                sep_list = [s[:end] for s in sep_list]
                ref_list = [s[:end] for s in ref_list]
            snr = permute_si_snr(sep_list, ref_list)
            reporter.add(key, snr)
            if details:
                details.write("{}\t{:.2f}\n".format(key, snr))
    reporter.report()
    if details:
        details.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=
        "Command to compute SI-SDR, as metric of the separation quality",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("sep_scp",
                        type=str,
                        help="Separated speech scripts, waiting for measure"
                        "(support multi-speaker, egs: spk1.scp,spk2.scp)")
    parser.add_argument("ref_scp",
                        type=str,
                        help="Reference speech scripts, as ground truth for"
                        " Si-SDR computation")
    parser.add_argument("--spk2class",
                        type=str,
                        default="",
                        help="If assigned, report results "
                        "per class (gender or degree)")
    parser.add_argument("--details",
                        type=str,
                        default="",
                        help="If assigned, report snr "
                        "improvement for each utterance")
    parser.add_argument("--sr",
                        type=int,
                        default=16000,
                        help="Sample rate of the audio")
    args = parser.parse_args()
    run(args)