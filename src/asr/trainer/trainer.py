# wujian@2019

import math
import random

from pathlib import Path
from collections import defaultdict

import numpy as np
import torch as th
import torch.nn.functional as F

from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.nn.utils import clip_grad_norm_
from torch.nn.parallel import data_parallel as datp
from torch.utils.tensorboard import SummaryWriter

import torch_complex.functional as cF

from .utils import get_logger, load_obj, get_device_ids, SimpleTimer
from .scheduler import support_ss_scheduler
from .noamopt import NoamOpt

IGNORE_ID = -1  # in data loader
EPSILON = np.finfo(np.float32).eps


def ce_loss(outs, tgts):
    """
    Cross entropy loss
    """
    _, _, V = outs.shape
    # N(To+1) x V
    outs = outs.view(-1, V)
    # N(To+1)
    tgts = tgts.view(-1)
    ce_loss = F.cross_entropy(outs,
                              tgts,
                              ignore_index=IGNORE_ID,
                              reduction="mean")
    return ce_loss


def ls_loss(outs, tgts, lsm_factor=0.1):
    """
    Label smooth loss (using KL)
    """
    _, _, V = outs.shape
    # NT x V
    outs = outs.view(-1, V)
    # NT
    tgts = tgts.view(-1)
    mask = (tgts != IGNORE_ID)
    # M x V
    outs = th.masked_select(outs, mask.unsqueeze(-1)).view(-1, V)
    # M
    tgts = th.masked_select(tgts, mask)
    # M x V
    dist = outs.new_full(outs.size(), lsm_factor / (V - 1))
    dist = dist.scatter_(1, tgts.unsqueeze(-1), 1 - lsm_factor)
    # KL distance
    loss = F.kl_div(F.log_softmax(outs, -1), dist, reduction="batchmean")
    return loss


def compute_accu(outs, tgts):
    """
    Compute frame-level accuracy
    """
    # N x (To+1)
    pred = th.argmax(outs.detach(), dim=-1)
    # ignore mask, -1
    mask = (tgts != IGNORE_ID)
    ncorr = th.sum(pred[mask] == tgts[mask]).item()
    total = th.sum(mask).item()
    return float(ncorr) / total


def process_tgts(tgt_pad, tgt_len, eos=0):
    """
    Process target labels for inference and loss computation
    """
    # N x To, -1 => EOS
    tgt_v1 = tgt_pad.masked_fill(tgt_pad == IGNORE_ID, eos)
    # N x (To+1), pad -1
    tgt_v2 = F.pad(tgt_pad, (0, 1), value=IGNORE_ID)
    # add eos
    tgt_v2 = tgt_v2.scatter(1, tgt_len[:, None], eos)
    return tgt_v1, tgt_v2


def add_gaussian_noise(nnet, std=0.075):
    """
    Add gaussian noise to updated weights
    """
    for p in nnet.parameters():
        if p.requires_grad:
            p.data += th.randn(p.data.shape, device=nnet.device) * std


class ProgressReporter(object):
    """
    A simple progress reporter
    """
    def __init__(self, checkpoint, period=100, tensorboard=True, rank=None):
        self.period = period
        if rank is None:
            logger_loc = (checkpoint / "trainer.log").as_posix()
            self.header = "Trainer"
        else:
            logger_loc = (checkpoint / f"trainer.rank{rank}.log").as_posix()
            self.header = f"Rank {rank}"

        self.logger = get_logger(logger_loc, file=True)
        if tensorboard:
            self.board_writer = SummaryWriter(checkpoint)
        else:
            self.board_writer = None
        self.reset()

    def log(self, sstr):
        self.logger.info(f"{self.header}: {sstr}")

    def eval(self):
        self.log(">> Set eval mode ...")
        self.mode = "valid"
        self.reset()

    def train(self):
        self.log(">> Set train mode ...")
        self.mode = "train"
        self.reset()

    def reset(self):
        self.stats = defaultdict(list)
        self.timer = SimpleTimer()

    def add(self, key, value):
        self.stats[key].append(value)
        N = len(self.stats[key])
        if not N % self.period:
            if key == "rate":
                cur = self.stats[key][-1]
                self.log(f"Processed {N:.2e} batches ({key} = {cur:.3e}) ...")
            else:
                avg = sum(self.stats[key][-self.period:]) / self.period
                self.log(f"Processed {N:.2e} batches ({key} = {avg:+.2f}) ...")

    def report(self, epoch, lr):
        N = len(self.stats["loss"])
        if self.mode == "valid":
            sstr = ",".join(
                map(lambda f: "{:.2f}".format(f), self.stats["loss"]))
            self.log(f"Loss on {N:d} batches: {sstr}")

        if N == 0:
            raise RuntimeError("No statistics to report")
        loss = sum(self.stats["loss"]) / N
        accu = sum(
            self.stats["accu"]) * 100 / N if "accu" in self.stats else None
        if self.board_writer:
            self.board_writer.add_scalar(f"loss/{self.mode}", loss, epoch)
            if accu is not None:
                self.board_writer.add_scalar(f"accu/{self.mode}", accu, epoch)
        cost = self.timer.elapsed()
        if accu is not None:
            hstr = f"Loss/Accu(time/N, lr={lr:.3e}) - Epoch {epoch:2d}: "
            cstr = f"{self.mode} = {loss:.4f}/{accu:.2f}({cost:.2f}m/{N:d})"
        else:
            hstr = f"Loss(time/N, lr={lr:.3e}) - Epoch {epoch:2d}: "
            cstr = f"{self.mode} = {loss:.4f}({cost:.2f}m/{N:d})"
        return loss, accu, hstr + cstr


class StopCriterion(object):
    """
    Early stop of the training
    """
    def __init__(self,
                 no_impr,
                 mode="min",
                 init_criterion=math.inf,
                 no_impr_thres=2e-3):
        self.max_no_impr = no_impr
        self.no_impr = 0
        self.no_impr_thres = no_impr_thres
        self.mode = mode
        self.best_criterion = init_criterion

    def reset(self, update_value):
        self.best_criterion = update_value

    def stop(self):
        return self.no_impr == self.max_no_impr

    def step(self, update_value):
        is_better = True
        # loss
        if self.mode == "min":
            is_better = self.best_criterion > update_value + self.no_impr_thres
        # accu
        if self.mode == "max":
            is_better = self.best_criterion < update_value - self.no_impr_thres
        if is_better:
            self.best_criterion = update_value
            self.no_impr = 0
            return True
        else:
            self.no_impr += 1
            return False


class Trainer(object):
    """
    A PyTorch base trainer
    """
    def __init__(self,
                 nnet,
                 checkpoint="cpt",
                 optimizer="adam",
                 device_ids=0,
                 optimizer_kwargs=None,
                 lr_scheduler="reduce",
                 lr_scheduler_kwargs=None,
                 ss_scheduler="const",
                 ss_scheduler_kwargs=None,
                 clip_gradient=None,
                 gaussian_noise_std=None,
                 prog_interval=100,
                 save_interval=-1,
                 resume="",
                 init="",
                 tensorboard=False,
                 stop_criterion="loss",
                 no_impr=6,
                 no_impr_thres=1e-3):
        self.device_ids = get_device_ids(device_ids)
        self.default_device = th.device(f"cuda:{self.device_ids[0]}")

        self.checkpoint = Path(checkpoint)
        self.checkpoint.mkdir(parents=True, exist_ok=True)
        self.reporter = ProgressReporter(self.checkpoint,
                                         period=prog_interval,
                                         tensorboard=tensorboard)
        self.clip_gradient = clip_gradient
        self.gaussian_noise_std = gaussian_noise_std
        self.cur_epoch = 0  # zero based
        self.save_interval = save_interval
        self.ssr = 0
        self.no_impr = no_impr

        mode = "max" if stop_criterion == "accu" else "min"
        self.stop_on = stop_criterion
        self.stop_criterion = StopCriterion(no_impr,
                                            mode=mode,
                                            no_impr_thres=no_impr_thres)

        self.reporter.log(f"Model summary:\n{nnet}")
        if resume or init:
            cpt_path = resume if resume else init
            if not Path(cpt_path).exists():
                raise FileNotFoundError(
                    f"Could not find checkpoint: {cpt_path}")
            cpt = th.load(cpt_path, map_location="cpu")
            self.cur_epoch = cpt["epoch"]
            nnet.load_state_dict(cpt["model_state_dict"])
            self.nnet = nnet.to(self.default_device)
            if resume:
                self.reporter.log(f"Resume from checkpoint {cpt_path}: " +
                                  f"epoch {self.cur_epoch}")
                self.optimizer = self.create_optimizer(
                    optimizer, optimizer_kwargs, state=cpt["optim_state_dict"])
            else:
                self.reporter.log(f"Intialized from checkpoint {cpt_path}: " +
                                  f"epoch {self.cur_epoch}")
                self.optimizer = self.create_optimizer(optimizer,
                                                       optimizer_kwargs)
        else:
            self.nnet = nnet.to(self.default_device)
            self.optimizer = self.create_optimizer(optimizer, optimizer_kwargs)
        if optimizer == "noam":
            self.lr_scheduler = None
            self.reporter.log("Noamopt: " + self.optimizer.info())
        else:
            self.lr_scheduler = ReduceLROnPlateau(self.optimizer,
                                                  mode=mode,
                                                  threshold_mode="abs",
                                                  threshold=no_impr_thres,
                                                  **lr_scheduler_kwargs)
        if ss_scheduler_kwargs:
            self.ss_scheduler = support_ss_scheduler(ss_scheduler,
                                                     **ss_scheduler_kwargs)
            self.reporter.log(f"Using schedule sampling: {ss_scheduler}")
        else:
            self.ss_scheduler = None
        self.num_params = sum(
            [param.nelement() for param in nnet.parameters()]) / 10.0**6

        # logging
        self.reporter.log(f"Loading model to GPUs:{self.device_ids}, " +
                          f"#param: {self.num_params:.2f}M")
        self.reporter.log(f"Stop criterion: {self.stop_on}")
        if clip_gradient:
            self.reporter.log(
                f"Gradient clipping if over {clip_gradient} L2 norm")
        if gaussian_noise_std:
            self.reporter.log("Add gaussian noise to network weights, " +
                              f"with std = {gaussian_noise_std}")

    def save_checkpoint(self, epoch, best=True):
        """
        Save checkpoint (epoch, model, optimizer)
        """
        cpt = {
            "epoch": epoch,
            "model_state_dict": self.nnet.state_dict(),
            "optim_state_dict": self.optimizer.state_dict()
        }
        cpt_name = "{}.pt.tar".format("best" if best else "last")
        th.save(cpt, self.checkpoint / cpt_name)
        self.reporter.log(f"Save checkpoint {cpt_name}")
        if self.save_interval > 0 and epoch % self.save_interval == 0:
            th.save(cpt, self.checkpoint / f"{epoch}.pt.tar")

    def create_optimizer(self, optimizer, kwargs, state=None):
        """
        Return a pytorch-optimizer
        """
        supported_optimizer = {
            "sgd": th.optim.SGD,  # momentum, weight_decay, lr
            "rmsprop": th.optim.RMSprop,  # momentum, weight_decay, lr
            "adam": th.optim.Adam,  # weight_decay, lr
            "adadelta": th.optim.Adadelta,  # weight_decay, lr
            "adagrad": th.optim.Adagrad,  # lr, lr_decay, weight_decay
            "adamax": th.optim.Adamax,  # lr, weight_decay
            "adamw": th.optim.AdamW,  # lr, weight_decay
            "noam": NoamOpt
            # ...
        }
        if optimizer not in supported_optimizer:
            raise ValueError(f"Unknown optimizer: {optimizer}")
        opt = supported_optimizer[optimizer](self.nnet.parameters(), **kwargs)
        self.reporter.log(f"Create optimizer {optimizer}: {kwargs}")
        if state is not None:
            opt.load_state_dict(state)
            self.reporter.log("Load optimizer state dict from checkpoint")
        return opt

    def compute_loss(self, egs, **kwargs):
        """
        Compute training loss, return loss and other numbers
        """
        raise NotImplementedError

    def train(self, data_loader):
        self.nnet.train()
        self.reporter.train()
        # for idx, egs in enumerate(data_loader):
        for egs in data_loader:
            # load to gpu
            egs = load_obj(egs, self.default_device)

            self.optimizer.zero_grad()

            loss = self.compute_loss(egs, ssr=self.ssr)
            loss.backward()

            # clip gradient after backward
            if self.clip_gradient:
                norm = clip_grad_norm_(self.nnet.parameters(),
                                       self.clip_gradient)

            loss = loss.item()
            if math.isfinite(norm) and math.isfinite(loss):
                self.optimizer.step()

                if self.gaussian_noise_std:
                    add_gaussian_noise(self.nnet, std=self.gaussian_noise_std)

                self.reporter.add("norm", norm)
                self.reporter.add("rate", self.optimizer.param_groups[0]["lr"])
            else:
                self.reporter.log(f"Invalid gradient {norm:.3f} or " +
                                  f"loss {loss:.3f}, skip...")

    def eval(self, data_loader):
        self.nnet.eval()
        self.reporter.eval()

        with th.no_grad():
            # for idx, egs in enumerate(data_loader):
            for egs in data_loader:
                egs = load_obj(egs, self.default_device)
                # ssr = 0, use ground truth
                _ = self.compute_loss(egs, ssr=0)

    def _prep_train(self, dev_loader):
        """
        Prepare for training
        """
        # avoid alloc memory from gpu0
        th.cuda.set_device(self.default_device)
        # make dilated conv faster
        th.backends.cudnn.benchmark = True
        # eval
        self.eval(dev_loader)
        e = self.cur_epoch
        best_loss, best_accu, _ = self.reporter.report(e, 0)
        if self.ss_scheduler:
            self.ssr = self.ss_scheduler.step(e, best_accu)
        # make sure not inf
        best_value = best_loss if self.stop_on == "loss" else best_accu
        if self.lr_scheduler:
            self.lr_scheduler.best = best_value
        self.stop_criterion.reset(best_value)
        # log here
        sstr = f"Epoch {e:d}, loss = {best_loss:.4f}"
        if best_accu is not None:
            sstr += f", accu = {best_accu:.2f}"
        self.reporter.log(sstr)
        return e

    def run(self, trn_loader, dev_loader, num_epoches=50):
        """
        Run on whole training set and evaluate
        """
        self.reporter.log(
            f"Number of batches (train/valid) = {len(trn_loader)}/{len(dev_loader)}"
        )
        e = self._prep_train(dev_loader)
        while e < num_epoches:
            e += 1
            cur_lr = self.optimizer.param_groups[0]["lr"]
            # >> train
            self.train(trn_loader)
            _, _, sstr = self.reporter.report(e, cur_lr)
            self.reporter.log(sstr)
            # << train
            # >> eval
            self.eval(dev_loader)
            cv_loss, cv_accu, sstr = self.reporter.report(e, cur_lr)
            # schedule sampling for eval
            if self.ss_scheduler:
                sstr += f" | ssr = {self.ssr:.3f}"

            update_value = cv_loss if self.stop_on == "loss" else cv_accu
            better = self.stop_criterion.step(update_value)
            if better:
                self.save_checkpoint(e, best=True)
            else:
                if self.lr_scheduler:
                    sstr += f" | no impr, best = {self.lr_scheduler.best:.4f}"
                else:
                    sstr += " | no impr"

            self.reporter.log(sstr)
            # << eval
            # schedule here
            if self.lr_scheduler:
                self.lr_scheduler.step(update_value)
            if self.ss_scheduler:
                self.ssr = self.ss_scheduler.step(e, cv_accu)
            # save last checkpoint
            self.save_checkpoint(e, best=False)
            # early stop
            if self.stop_criterion.stop():
                self.reporter.log("Stop training cause no impr for " +
                                  f"{self.no_impr} epochs")
                break
        self.reporter.log(f"Training for {e:d}/{num_epoches:d} epoches done!")

    def run_batch_per_epoch(self,
                            trn_loader,
                            dev_loader,
                            num_epoches=100,
                            eval_interval=4000):
        """
        Run on several batches and evaluate
        """
        self.reporter.log(
            f"Number of batches (train/valid) = {len(trn_loader)}/{len(dev_loader)}"
        )
        e = self._prep_train(dev_loader)
        stop = False
        trained_batches = 0
        # set train mode
        self.nnet.train()
        self.reporter.train()
        while True:
            # trained on several batches
            # for idx, egs in enumerate(trn_loader):
            for egs in trn_loader:
                trained_batches = (trained_batches + 1) % eval_interval
                # update per-batch
                egs = load_obj(egs, self.default_device)
                self.optimizer.zero_grad()

                loss = self.compute_loss(egs, ssr=self.ssr)
                loss.backward()

                if self.clip_gradient:
                    norm = clip_grad_norm_(self.nnet.parameters(),
                                           self.clip_gradient)
                loss = loss.item()
                if math.isfinite(norm) and math.isfinite(loss):
                    self.optimizer.step()

                    if self.gaussian_noise_std:
                        add_gaussian_noise(self.nnet,
                                           std=self.gaussian_noise_std)

                    self.reporter.add("norm", norm)
                    self.reporter.add("rate",
                                      self.optimizer.param_groups[0]["lr"])
                else:
                    self.reporter.log(f"Invalid gradient {norm:.3f} or " +
                                      f"loss {loss:.3f}, skip...")

                # if trained on batches done, start evaluation
                if trained_batches == 0:
                    e += 1
                    cur_lr = self.optimizer.param_groups[0]["lr"]
                    _, _, sstr = self.reporter.report(e, cur_lr)
                    self.reporter.log(sstr)

                    cv_loss, cv_accu, sstr = self.reporter.report(e, cur_lr)
                    # schedule sampling for eval
                    if self.ss_scheduler:
                        sstr += f" | ssr = {self.ssr:.3f}"

                    update_value = cv_loss if self.stop_on == "loss" else cv_accu
                    better = self.stop_criterion.step(update_value)
                    if better:
                        self.save_checkpoint(e, best=True)
                    else:
                        if self.lr_scheduler:
                            sstr += f" | no impr, best = {self.lr_scheduler.best:.4f}"
                        else:
                            sstr += " | no impr"

                    self.reporter.log(sstr)
                    # schedule here
                    if self.lr_scheduler:
                        self.lr_scheduler.step(update_value)
                    self.ssr = self.ss_scheduler.step(e, cv_accu)
                    # save last checkpoint
                    self.save_checkpoint(e, best=False)
                    # reset reporter
                    self.reporter.reset()
                    # early stop or not
                    if self.stop_criterion.stop():
                        self.reporter.log("Stop training cause no impr for " +
                                          f"{self.no_impr} epochs")
                        stop = True
                        break
                    if e == num_epoches:
                        stop = True
                        break
                    # enable train mode
                    self.reporter.log("Set train mode...")
                    self.nnet.train()
            self.reporter.log("Finished one epoch on training set")
            if stop:
                break
        self.reporter.log(f"Training for {e:d}/{num_epoches:d} epoches done!")


class CtcXentHybridTrainer(Trainer):
    """
    ASR Trainer (CTC & CE)
    """
    def __init__(self,
                 nnet,
                 lsm_factor=0,
                 ctc_regularization=0,
                 ctc_blank=0,
                 **kwargs):
        super(CtcXentHybridTrainer, self).__init__(nnet, **kwargs)
        if ctc_regularization:
            self.reporter.log(
                f"Use CTC regularization (factor = {ctc_regularization:.2f}" +
                f", blank = {ctc_blank})")
        self.ctc_blank = ctc_blank
        self.ctc_factor = ctc_regularization
        self.lsm_factor = lsm_factor

    def compute_loss(self, egs, ssr=0, **kwargs):
        """
        Compute training loss, egs contains:
            src_pad: N x Ti x F
            src_len: N
            tgt_pad: N x To
            tgt_len: N
        """
        # tgt_pad: N x To (replace ignore_id with eos)
        # tgts: N x To+1 (add eos)
        tgt_pad, tgts = process_tgts(egs["tgt_pad"],
                                     egs["tgt_len"],
                                     eos=self.nnet.eos)
        # outs: N x (To+1) x V
        # alis: N x (To+1) x Ti
        pack = (egs["src_pad"], egs["src_len"], tgt_pad, ssr)
        outs, _, ctc_branch, enc_len = datp(self.nnet,
                                            pack,
                                            device_ids=self.device_ids)
        # compute loss
        if self.lsm_factor > 0:
            loss = ls_loss(outs, tgts, lsm_factor=self.lsm_factor)
        else:
            loss = ce_loss(outs, tgts)

        if self.ctc_factor > 0:
            # add log-softmax, N x T x V => T x N x V
            log_prob = F.log_softmax(ctc_branch, dim=-1).transpose(0, 1)
            # CTC loss
            ctc_loss = F.ctc_loss(log_prob,
                                  tgt_pad,
                                  enc_len,
                                  egs["tgt_len"],
                                  blank=self.ctc_blank,
                                  reduction="mean",
                                  zero_infinity=True)
            loss = self.ctc_factor * ctc_loss + (1 - self.ctc_factor) * loss
            self.reporter.add("fctc", ctc_loss.item())
        # compute accu
        accu = compute_accu(outs, tgts)
        # add to reporter
        self.reporter.add("loss", loss.item())
        self.reporter.add("accu", accu)
        return loss


# from https://github.com/HawkAaron/warp-transducer
# from warprnnt_pytorch import rnnt_loss
# https://github.com/1ytic/warp-rnnt
from warp_rnnt import rnnt_loss


class TransducerTrainer(Trainer):
    """
    ASR Trainer (Transducer)
    """
    def __init__(self, nnet, transducer_blank=0, **kwargs):
        super(TransducerTrainer, self).__init__(nnet, **kwargs)
        self.blank = transducer_blank
        self.reporter.log(
            f"Got Transducer trainer, blank = {transducer_blank}")

    def compute_loss(self, egs, **kwargs):
        """
        Compute training loss, egs contains:
            src_pad: N x Ti x F
            src_len: N
            tgt_pad: N x To
            tgt_len: N
        """
        # tgt_pad: N x To (replace ignore_id with blank)
        ignore_mask = egs["tgt_pad"] == IGNORE_ID
        tgt_pad = egs["tgt_pad"].masked_fill(ignore_mask, self.blank)
        # N x Ti x To+1 x V
        pack = (egs["src_pad"], egs["src_len"], tgt_pad, egs["tgt_len"])
        outs, enc_len = datp(self.nnet, pack, device_ids=self.device_ids)
        # add log_softmax if use https://github.com/1ytic/warp-rnnt
        outs = F.log_softmax(outs, -1)
        # compute loss
        loss = rnnt_loss(outs,
                         tgt_pad.to(th.int32),
                         enc_len.to(th.int32),
                         egs["tgt_len"].to(th.int32),
                         blank=self.blank,
                         reduction="mean",
                         gather=True)
        # add to reporter
        self.reporter.add("loss", loss.item())
        return loss


class LmTrainer(Trainer):
    """
    LM Trainer (CE)
    """
    def __init__(self, *args, repackage_hidden=False, **kwargs):
        super(LmTrainer, self).__init__(*args, **kwargs)
        self.hidden = None
        self.repackage_hidden = repackage_hidden
        self.reporter.log("Repackage hidden state for each batch")

    def compute_loss(self, egs, **kwargs):
        """
        Compute training loss, egs contains
            src: N x T+1
            tgt: N x T+1
            len: N
        """
        # pred: N x T+1 x V
        if self.repackage_hidden:
            pack = (egs["src"], self.hidden)
            pred, self.hidden = datp(self.nnet,
                                     pack,
                                     device_ids=self.device_ids)
        else:
            pack = (egs["src"], None, egs["len"])
            pred, _ = datp(self.nnet, pack, device_ids=self.device_ids)
        loss = ce_loss(pred, egs["tgt"])
        accu = compute_accu(pred, egs["tgt"])
        # add to reporter
        self.reporter.add("loss", loss.item())
        self.reporter.add("accu", accu)
        return loss


class MlTrainer(Trainer):
    """
    Unsupervised trainer using max-likelihood functions
    """
    def __init__(self, *args, **kwargs):
        super(MlTrainer, self).__init__(*args, **kwargs)

    def estimate_covar(self, mask, obs, eps=EPSILON):
        """
        Covariance matrices estimation
        args:
            mask: TF-masks (real), N x F x T
            obs: complex, N x F x C x T
        return:
            covar: complex, N x F x C x C
        """
        _, _, C, _ = obs.shape
        # N x F x 1 x T
        mask = mask.unsqueeze(-2)
        # N x F x C x C
        nominator = cF.einsum("...it,...jt->...ij", [obs * mask, obs.conj()])
        # N x F x 1 x 1
        denominator = th.clamp(mask.sum(-1, keepdims=True), min=eps)
        # N x F x C x C
        Bk = C * nominator / denominator
        # N x F x C x C
        Bk = (Bk + Bk.transpose(-1, -2).conj()) / 2
        return Bk

    def det(self, Bk, eps=EPSILON):
        """
        Compute determinant of the hermitian matrices
        args:
            Bk: N x F x C x C
        return:
            det: N x F
        """
        # N x F x C x 2C
        m = th.cat([Bk.real, -Bk.imag], -1)
        # N x F x C x 2C
        n = th.cat([Bk.imag, Bk.real], -1)
        # N x F x 2C x 2C
        Rk = th.cat([m, n], -2)
        # N x F x 2C
        # eigenvectors=False can not backward error
        ev, _ = th.symeig(Rk, eigenvectors=True)
        # N x F x C
        det = th.cumprod(ev[..., ::2], dim=-1)
        # N x F, non-negative
        det = th.clamp(det[..., -1], min=eps)
        return det

    def log_pdf(self, mask, obs, eps=EPSILON):
        """
        Compute log-pdf of the cacgmm distributions
        args:
            mask: TF-masks (real), N x F x T
            obs: complex, N x F x C x T
        """
        _, _, C, _ = obs.shape
        # N x F x C x C
        Bk = self.estimate_covar(mask, obs, eps=eps)
        # add to diag
        I = th.eye(C, device=Bk.device, dtype=Bk.dtype)
        Bk = Bk + I * eps
        # N x F
        Dk = self.det(Bk, eps=eps)
        # N x F x C x C
        Bk_inv = Bk.inverse()
        # N x F x T
        K = cF.einsum("...xt,...xy,...yt->...t", [obs.conj(), Bk_inv, obs])
        K = th.clamp(K.real, min=eps)
        # N x F x T
        log_pdf = -C * th.log(K) - th.log(Dk[..., None])
        # N x F x T
        return log_pdf

    def compute_loss(self, egs, eps=1e-6, **kwargs):
        # mag, pha: N x C x F x T
        # ms: N x T x F
        obs, ms = self.nnet(egs["mix"])
        # N x F x C x T
        obs = obs.transpose(1, 2)
        # N x F x T
        ms = ms.transpose(-1, -2)
        # N x F x T
        ps = self.log_pdf(ms, obs, eps=eps)
        pn = self.log_pdf(1 - ms, obs, eps=eps)
        # N x F x T
        log_pdf = th.log((th.exp(ps) + th.exp(pn)) * 0.5)
        # to maxinmum log_pdf
        loss = -th.mean(log_pdf)
        # add to reporter
        self.reporter.add("loss", loss.item())
        return loss