#!/usr/bin/env python3
"""Detector facial propio desde cero (Fase 8). Single-stage multi-escala con anclas,
entrenado en WIDER train global, NMS propio, sin pesos preentrenados.
Politica de cajas: degeneradas (w/h<=0) excluidas de todo; invalid=1 -> regiones
ignoradas (ni positivas ni fondo). 4 imgs sin caras como negativos.
Uso: --smoke (200 imgs, 2 epocas) | normal (completo).
"""
import os, sys, json, time, glob, random, argparse, math
import warnings as _warnings
_warnings.filterwarnings("ignore", message=".*weights_only.*", category=FutureWarning)
from collections import defaultdict
import numpy as np
from PIL import Image
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

SEED = 42
random.seed(SEED); np.random.seed(SEED); torch.manual_seed(SEED)
if torch.cuda.is_available(): torch.cuda.manual_seed_all(SEED)
torch.backends.cudnn.deterministic = True; torch.backends.cudnn.benchmark = False

IN_W, IN_H = 640, 480
STRIDES = [8, 16, 32]
ANCHORS = {8: [16, 32, 64], 16: [32, 64, 128], 32: [64, 128, 256]}
POS_IOU, NEG_IOU = 0.5, 0.4
MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
STD = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)

def parse_gt(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    recs = {}; i = 0
    while i < len(lines):
        fn = lines[i].strip(); n = int(lines[i + 1].strip())
        boxes = []
        for k in range(max(n, 1)):
            p = lines[i + 2 + k].strip().split()
            if n > 0 and len(p) >= 10:
                boxes.append(tuple(int(v) for v in p[:10]))
        recs[fn] = boxes; i += 2 + max(n, 1)
    return recs

def build_anchors():
    anc = {}
    for st in STRIDES:
        gw, gh = IN_W // st, IN_H // st
        ys, xs = torch.meshgrid(torch.arange(gh), torch.arange(gw), indexing="ij")
        cx = (xs.float() + 0.5) * st; cy = (ys.float() + 0.5) * st
        per = []
        for s in ANCHORS[st]:
            per.append(torch.stack([cx, cy, torch.full_like(cx, float(s)), torch.full_like(cy, float(s))], -1))
        anc[st] = torch.stack(per, 0)  # (A,H,W,4) cxcywh
    return anc
ANCH = build_anchors()

def box_iou(a, b):
    # a:(N,4) b:(M,4) xyxy
    ax1, ay1, ax2, ay2 = a[:, 0:1], a[:, 1:2], a[:, 2:3], a[:, 3:4]
    bx1, by1, bx2, by2 = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    ix1 = torch.maximum(ax1, bx1); iy1 = torch.maximum(ay1, by1)
    ix2 = torch.minimum(ax2, bx2); iy2 = torch.minimum(ay2, by2)
    iw = (ix2 - ix1).clamp(min=0); ih = (iy2 - iy1).clamp(min=0)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua.clamp(min=1e-6)

def cxcywh_to_xyxy(b):
    x, y, w, h = b.unbind(-1)
    return torch.stack([x - w / 2, y - h / 2, x + w / 2, y + h / 2], -1)

def flat_anchors(s):
    return ANCH[s].permute(1, 2, 0, 3).reshape(-1, 4)  # ((h*W+w)*A+a) igual que scores/deltas

class WiderDataset(Dataset):
    def __init__(self, files, gt_by_file, imgdir):
        self.items = [(f, gt_by_file.get(f, [])) for f in files]
        self.imgdir = imgdir
    def __len__(self): return len(self.items)
    def __getitem__(self, i):
        rel, boxes = self.items[i]
        im = Image.open(os.path.join(self.imgdir, rel)).convert("RGB")
        W0, H0 = im.size
        im = im.resize((IN_W, IN_H), Image.BILINEAR)
        t = torch.from_numpy(np.asarray(im, dtype=np.float32) / 255.0).permute(2, 0, 1)
        t = (t - MEAN) / STD
        sx, sy = IN_W / W0, IN_H / H0
        valid, ign = [], []
        for b in boxes:
            x, y, w, h = b[:4]
            if w <= 0 or h <= 0: continue  # degenerada: fuera de todo
            b2 = [x * sx, y * sy, w * sx, h * sy]
            (ign if b[7] == 1 else valid).append(b2)
        return t, (torch.tensor(valid, dtype=torch.float32).reshape(-1, 4),
                   torch.tensor(ign, dtype=torch.float32).reshape(-1, 4), rel)

def encode_targets(anchors_xyxy, valid_xyxy, ign_xyxy):
    """Devuelve por ancla: obj_target (1/0/-1 ignore), box_target (dx,dy,dw,dh)."""
    N = anchors_xyxy.shape[0]
    dev = anchors_xyxy.device
    obj = torch.zeros(N, device=dev); tgt = torch.zeros(N, 4, device=dev)
    if valid_xyxy.numel() == 0:
        if ign_xyxy.numel():
            iou_i = box_iou(anchors_xyxy, ign_xyxy)
            obj[iou_i.max(1).values >= 0.5] = -1
        return obj, tgt
    iou = box_iou(anchors_xyxy, valid_xyxy)
    best, arg = iou.max(1)
    pos = best >= POS_IOU
    amb = (best >= NEG_IOU) & (best < POS_IOU)
    obj[pos] = 1; obj[amb] = -1
    if ign_xyxy.numel():
        iou_i = box_iou(anchors_xyxy, ign_xyxy)
        obj[iou_i.max(1).values >= 0.5] = -1
        obj[pos & (iou_i.max(1).values >= 0.5)] = -1
    if pos.any():
        aw = anchors_xyxy[pos]
        acx = (aw[:, 0] + aw[:, 2]) / 2; acy = (aw[:, 1] + aw[:, 3]) / 2
        aww = (aw[:, 2] - aw[:, 0]).clamp(min=1.0); ahh = (aw[:, 3] - aw[:, 1]).clamp(min=1.0)
        gv = valid_xyxy[arg[pos]]
        gx = (gv[:, 0] + gv[:, 2]) / 2 - acx
        gy = (gv[:, 1] + gv[:, 3]) / 2 - acy
        tgt[pos, 0] = gx / aww; tgt[pos, 1] = gy / ahh
        tgt[pos, 2] = torch.log(((gv[:, 2] - gv[:, 0]) / aww).clamp(min=1e-3))
        tgt[pos, 3] = torch.log(((gv[:, 3] - gv[:, 1]) / ahh).clamp(min=1e-3))
    return obj, tgt

class Mix(Dataset):
    """Dataset con dirs mixtos: recibe lista (rel, dir, boxes). Nivel de modulo para workers."""
    def __init__(self, items):
        self.d = items
    def __len__(self): return len(self.d)
    def __getitem__(self, i):
        rel, d, boxes = self.d[i]
        im = Image.open(os.path.join(d, rel)).convert("RGB")
        W0, H0 = im.size
        t = torch.from_numpy(np.asarray(im.resize((IN_W, IN_H), Image.BILINEAR), dtype=np.float32) / 255.0).permute(2, 0, 1)
        t = (t - MEAN) / STD
        sx, sy = IN_W / W0, IN_H / H0
        valid, ign = [], []
        for b in boxes:
            x, y, w, h = b[:4]
            if w <= 0 or h <= 0: continue
            bxy = [x * sx, y * sy, (x + w) * sx, (y + h) * sy]  # xyxy en 640x480
            (ign if b[7] == 1 else valid).append(bxy)
        return t, (torch.tensor(valid, dtype=torch.float32).reshape(-1, 4),
                   torch.tensor(ign, dtype=torch.float32).reshape(-1, 4), rel)


def worker_init_fn(worker_id):
    import random as _r, numpy as _np
    _r.seed(SEED + worker_id); _np.random.seed(SEED + worker_id)


def collate_mix(batch):
    return ([x[0] for x in batch], [x[1] for x in batch])


class Backbone(nn.Module):
    def __init__(self):
        super().__init__()
        def blk(ci, co, s):
            return nn.Sequential(nn.Conv2d(ci, co, 3, stride=s, padding=1), nn.BatchNorm2d(co), nn.ReLU(inplace=True),
                                 nn.Conv2d(co, co, 3, padding=1), nn.BatchNorm2d(co), nn.ReLU(inplace=True))
        self.c1 = blk(3, 32, 2)    # 320x240
        self.c2 = blk(32, 64, 2)   # 160x120
        self.c3 = blk(64, 128, 2)  # 80x60  (s8)
        self.c4 = blk(128, 256, 2) # 40x30  (s16)
        self.c5 = blk(256, 256, 2) # 20x15  (s32)
    def forward(self, x):
        x = self.c1(x); x = self.c2(x); f8 = self.c3(x); f16 = self.c4(f8); f32 = self.c5(f16)
        return {8: f8, 16: f16, 32: f32}

class Detector(nn.Module):
    def __init__(self, n_anc=3):
        super().__init__()
        self.back = Backbone()
        self.obj = nn.ModuleDict({str(s): nn.Conv2d({8: 128, 16: 256, 32: 256}[s], n_anc, 1) for s in STRIDES})
        self.box = nn.ModuleDict({str(s): nn.Conv2d({8: 128, 16: 256, 32: 256}[s], n_anc * 4, 1) for s in STRIDES})
        for m in [self.obj, self.box]:
            for c in m.values(): nn.init.normal_(c.weight, std=0.01); nn.init.zeros_(c.bias)
    def forward(self, x):
        f = self.back(x); out = {}
        for s in STRIDES:
            k = str(s)
            out[s] = (self.obj[k](f[s]), self.box[k](f[s]))
        return out

def decode(anc_cxcywh, deltas):
    ax, ay, aw, ah = anc_cxcywh.unbind(-1)
    px = deltas[..., 0] * aw + ax; py = deltas[..., 1] * ah + ay
    pw = torch.exp(deltas[..., 2].clamp(max=6)) * aw; ph = torch.exp(deltas[..., 3].clamp(max=6)) * ah
    return torch.stack([px - pw / 2, py - ph / 2, px + pw / 2, py + ph / 2], -1)

def nms(boxes, scores, thr=0.5, topk=500):
    if boxes.numel() == 0: return torch.zeros(0, dtype=torch.long)
    order = scores.argsort(descending=True)[:topk * 2]
    boxes, scores, order = boxes[order], scores[order], order
    keep = []
    while order.numel():
        keep.append(order[0].item());
        if order.numel() == 1: break
        iou = box_iou(boxes[0:1], boxes[1:]).squeeze(0)
        order = order[1:][iou <= thr]
        boxes = boxes[1:][iou <= thr]; scores = scores[1:]
    return torch.tensor(keep[:topk], dtype=torch.long)

@torch.no_grad()
def infer(model, img_t, thr=0.3, nms_thr=0.5):
    model.eval()
    device = next(model.parameters()).device
    outs = model(img_t.unsqueeze(0).to(device))
    all_b, all_s = [], []
    for s in STRIDES:
        o, d = outs[s]
        A = len(ANCHORS[s])
        scores = torch.sigmoid(o[0]).permute(1, 2, 0).reshape(-1, A)
        delt = d[0].permute(1, 2, 0).reshape(-1, A, 4)
        anc = ANCH[s].permute(1,2,0,3).to(device).reshape(-1, A, 4)
        boxes = decode(anc, delt).reshape(-1, 4)
        scores = scores.reshape(-1)
        m = scores > 0.05
        all_b.append(boxes[m]); all_s.append(scores[m])
    if not all_b: return torch.zeros(0, 4), torch.zeros(0)
    B = torch.cat(all_b); S = torch.cat(all_s)
    B[:, 0::2].clamp_(0, IN_W); B[:, 1::2].clamp_(0, IN_H)
    k = nms(B, S, nms_thr)
    m2 = S[k] >= thr
    return B[k][m2].cpu(), S[k][m2].cpu()

def _ensure_venv():
    """Si el lanzador ejecuto el interprete del sistema, re-ejecuta en el venv (mismo PID)."""
    import sys as _s
    if _s.platform != "win32" or __import__("os").environ.get("CENTINELA_REEXEC") == "1":
        return
    exe = _s.executable.replace("/", "\\")
    if exe.endswith("Python312\\python.exe") and "\\.venv\\" not in exe:
        import os as _o
        root = _o.environ.get("VIRTUAL_ENV", r"C:\Zero\Dev\centinela-vial\.venv")
        venv_py = _o.path.join(root, "Scripts", "python.exe")
        if _o.path.isfile(venv_py):
            print("re-exec en venv: %s" % venv_py, flush=True)
            _o.environ["CENTINELA_REEXEC"] = "1"
            _o.execv(venv_py, [venv_py] + _s.argv)
        else:
            print("AVISO: sin venv disponible, sigo con %s" % exe, flush=True)


def save_atomic(obj, path):
    tmp = path + ".tmp"
    torch.save(obj, tmp)
    os.replace(tmp, path)


def rng_states():
    st = {"py": random.getstate(), "np": np.random.get_state(), "torch": torch.get_rng_state()}
    if torch.cuda.is_available():
        st["cuda"] = torch.cuda.get_rng_state_all()
    return st


def rng_restore(st):
    random.setstate(st["py"]); np.random.set_state(st["np"]); torch.set_rng_state(st["torch"])
    if torch.cuda.is_available() and "cuda" in st:
        torch.cuda.set_rng_state_all(st["cuda"])


CFG_KEYS = ["in", "strides", "anchors", "pos_iou", "neg_iou", "manifiesto", "politica", "batch", "lr"]


def check_cfg_compatible(saved_cfg, cfg, batch, lr):
    diffs = [k for k in CFG_KEYS if saved_cfg.get(k) != cfg.get(k)]
    if saved_cfg.get("batch") != batch:
        diffs.append("batch(arg)")
    if saved_cfg.get("lr") != lr:
        diffs.append("lr(arg)")
    if cfg.get("in") != [IN_W, IN_H]:
        diffs.append("in(esperado %s)" % [IN_W, IN_H])
    return diffs


def acquire_lock(exp_dir):
    """Una sola instancia por experimento. Devuelve True si somos duenos."""
    path = os.path.join(exp_dir, ".lock")
    os.makedirs(exp_dir, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        return True
    except FileExistsError:
        try:
            pid = int(open(path).read().strip())
        except Exception:
            pid = None
        alive = False
        if pid:
            try:
                os.kill(pid, 0)
                alive = True
            except Exception:
                alive = False
        if alive:
            return False
        with open(path, "w") as f:
            f.write(str(os.getpid()))
        return True


def release_lock(exp_dir):
    try:
        if open(os.path.join(exp_dir, ".lock")).read().strip() == str(os.getpid()):
            os.remove(os.path.join(exp_dir, ".lock"))
    except Exception:
        pass


def log_event(exp_dir, msg):
    from datetime import datetime
    line = "%s | %s" % (datetime.now().astimezone().isoformat(), msg)
    print(line, flush=True)
    if exp_dir:
        with open(os.path.join(exp_dir, "training.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


def append_log(exp_dir, ep_seg, hist, seconds, extra=None):
    line = {"epoch_seg": ep_seg, "train_loss": hist["train"][-1], "val_loss": hist["val"][-1],
            "segundos": round(seconds, 1)}
    if extra:
        line.update(extra)
    with open(os.path.join(exp_dir, "historial.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
        f.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--epochs", type=int, default=25)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--thr", type=float, default=0.3)
    ap.add_argument("--init", default=None, metavar="RUTA",
                    help="Warm-start: carga solo pesos y comienza un segmento nuevo")
    ap.add_argument("--resume", default=None, metavar="RUTA",
                    help="Reanuda un segmento (restaura pesos, optimizador, scheduler, epoca, metricas, RNG)")
    ap.add_argument("--exp-dir", default=None,
                    help="Carpeta del experimento (por defecto models/ocular_v2 para segmento 1)")
    ap.add_argument("--patience", type=int, default=6)
    ap.add_argument("--cpu", action="store_true", help="Permitir CPU (por defecto exige CUDA)")
    a = ap.parse_args()
    if a.init and a.resume:
        sys.exit("[ERROR] --init y --resume son excluyentes")
    _ensure_venv()
    if a.batch < 1 or a.epochs < 1:
        sys.exit("[ERROR] batch y epochs deben ser >= 1 (batch=%s epochs=%s)" % (a.batch, a.epochs))
    mode = "resume" if a.resume else ("warm-start" if a.init else "segmento-1")
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    import sys as _sys
    print("interprete: %s | torch=%s | device=%s" % (_sys.executable, torch.__version__, device), flush=True)
    if device.type != "cuda" and not a.cpu:
        sys.exit("[ERROR] se requiere CUDA en este equipo; el interprete actual no la ofrece. Usa --cpu para forzar CPU.")
    man = json.load(open("data/external_derived/wider/particion_global_v1.json", encoding="utf-8"))
    gtt = parse_gt("data/external_raw/wider/wider_face_split/wider_face_train_bbx_gt.txt")
    gtv = parse_gt("data/external_raw/wider/wider_face_split/wider_face_val_bbx_gt.txt")
    gt_all = {**gtt, **gtv}
    def imgdir(split_of_rel):
        return "data/external_raw/wider/WIDER_train/images" if man["origen"][split_of_rel]["wider_oficial"] == "WIDER_train" else "data/external_raw/wider/WIDER_val/images"
    # Agrupar por dir oficial para dataset (misma transform; dir por item)
    tr_rel, va_rel, te_rel = man["train"], man["val"], man["test"]
    if a.smoke:
        tr_rel, va_rel = tr_rel[:200], va_rel[:50]
    tr_items = [(r, imgdir(r), gt_all.get(r, [])) for r in tr_rel]
    va_items = [(r, imgdir(r), gt_all.get(r, [])) for r in va_rel]
    tr_ds, va_ds = Mix(tr_items), Mix(va_items)
    _dl = {"batch_size": a.batch, "num_workers": 4, "pin_memory": True, "persistent_workers": False,
           "worker_init_fn": worker_init_fn,
           "collate_fn": collate_mix}
    tr_l = DataLoader(tr_ds, shuffle=True, **_dl)
    va_l = DataLoader(va_ds, shuffle=False, **_dl)
    def batch_loss(ts, tgts, model, bce, flat_anc, device):
        X = torch.stack(ts).to(device)
        outs = model(X)
        total, nsamp = 0.0, len(ts)
        for bi in range(nsamp):
            valid, ign, rel = tgts[bi]
            li = 0.0
            for s in STRIDES:
                o, d = outs[s]
                A = len(ANCHORS[s])
                so = o[bi].permute(1, 2, 0).reshape(-1)
                dl = d[bi].permute(1, 2, 0).reshape(-1, A, 4).reshape(-1, 4)
                anc = flat_anc[s].to(device)
                obj, tgt = encode_targets(anc, valid.to(device), ign.to(device))
                keep = obj >= 0
                lo = (bce(so[keep], obj[keep]).mean() if keep.any() else torch.zeros((), device=device))
                posm = obj == 1
                lb = (F.smooth_l1_loss(dl[posm], tgt[posm], reduction="mean") if posm.any() else torch.zeros((), device=device))
                li = li + lo + lb
            total = total + li / len(STRIDES)
        return total / nsamp
    bce = nn.BCEWithLogitsLoss(reduction="none")
    flat_anc = {s: cxcywh_to_xyxy(flat_anchors(s)) for s in STRIDES}
    model = Detector().to(device)
    opt = torch.optim.Adam(model.parameters(), lr=a.lr)
    sched = None
    cfg = {"seed": SEED, "in": [IN_W, IN_H], "strides": STRIDES, "anchors": ANCHORS,
           "pos_iou": POS_IOU, "neg_iou": NEG_IOU, "manifiesto": "particion_global_v1.json",
           "politica": "degeneradas excluidas; invalid=1 ignoradas (ni pos ni fondo); 4 sin-caras como negativos",
           "batch": a.batch, "lr": a.lr, "epochs": a.epochs}
    exp_dir = a.exp_dir or "models/ocular_v2"
    os.makedirs(exp_dir, exist_ok=True)
    if not a.smoke:
        if not acquire_lock(exp_dir):
            sys.exit("[ERROR] ya hay una instancia entrenando en %s (lock activo). No se lanza duplicado." % exp_dir)
        print("lock adquirido: %s (pid %d)" % (os.path.join(exp_dir, ".lock"), os.getpid()), flush=True)
    if mode == "segmento-1":
        ckpt = os.path.join(exp_dir, "detector_facial_wider_v1.pth")
        start_ep, best, wait, hist = 0, float("inf"), 0, {"train": [], "val": []}
        provenance = {"segmento": 1, "modo": mode}
    else:
        src = a.resume or a.init
        if not os.path.isfile(src):
            sys.exit("[ERROR] no existe: %s" % src)
        saved = torch.load(src, map_location="cpu")  # cpu: preserva RNG ByteTensor; el modelo ya esta en device
        if "model_state_dict" not in saved:
            sys.exit("[ERROR] checkpoint sin pesos: %s" % src)
        saved_cfg = saved.get("cfg", {})
        diffs = check_cfg_compatible(saved_cfg, cfg, a.batch, a.lr)
        if diffs:
            sys.exit("[ERROR] configuracion incompatible con %s: %s" % (src, diffs))
        model.load_state_dict(saved["model_state_dict"])
        print("[%s] pesos cargados de %s" % (mode, src))
        if mode == "warm-start":
            # Segmento nuevo: optimizador y scheduler nuevos; referencia = val del modelo inicial
            opt = torch.optim.Adam(model.parameters(), lr=a.lr)
            sched = torch.optim.lr_scheduler.ReduceLROnPlateau(opt, mode="min", factor=0.5, patience=3)
            model.eval()
            with torch.no_grad():
                v0, n0 = 0.0, 0
                for ts, tgts in va_l:
                    v0 += batch_loss(ts, tgts, model, bce, flat_anc, device).item(); n0 += 1
            best, wait = v0 / max(n0, 1), 0
            start_ep, hist = 0, {"train": [], "val": [best]}
            print("[warm-start] val inicial=%.4f (referencia best)" % best)
            provenance = {"segmento": 2, "modo": mode, "init_ref": src, "init_val": best,
                          "nota": "warm-start: optimizador/scheduler/RNG reiniciados; no es continuacion exacta"}
        else:
            for k in ("optimizer_state_dict", "scheduler_state_dict", "epoch", "best", "wait", "rng"):
                if k not in saved:
                    sys.exit("[ERROR] %s no es reanudable (falta %s)" % (src, k))
            opt.load_state_dict(saved["optimizer_state_dict"])
            sched = torch.optim.lr_scheduler.ReduceLROnPlateau(opt, mode="min", factor=0.5, patience=3)
            sched.load_state_dict(saved["scheduler_state_dict"])
            rng_restore(saved["rng"])
            start_ep, best, wait = saved["epoch"] + 1, saved["best"], saved["wait"]
            hist = saved.get("history", {"train": [], "val": []})
            print("[resume] continua en epoca-seg %d (best=%.4f wait=%d)" % (start_ep, best, wait))
            provenance = {"segmento": saved.get("provenance", {}).get("segmento", "?"), "modo": mode,
                          "resume_ref": src}
        ckpt = None  # se guarda en exp_dir como last/best
    n_epochs = a.epochs if not a.smoke else min(a.epochs, 2)
    import time as _time
    from datetime import datetime as _dt
    _seg_label = "1" if mode == "segmento-1" else str(provenance.get("segmento", 2))
    _logdir = None if mode == "segmento-1" else exp_dir
    _tty = sys.stdout.isatty()
    if mode != "segmento-1" and not _tty:
        log_event(_logdir, "aviso: stdout no interactivo; progreso dinamico desactivado, latido cada 60 s")
    n_tr_batches = len(tr_l)
    _interrumpido = False
    try:
        import importlib.util as _tu
        _tp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "comun", "tablero.py")
        _ts = _tu.spec_from_file_location("tablero", _tp)
        _tm = _tu.module_from_spec(_ts); _ts.loader.exec_module(_tm)
        _tab = _tm.Tablero("Detector facial seg-%s" % _seg_label,
                           total_epocas=start_ep + n_epochs, batches=n_tr_batches)
    except Exception:
        _tab = None
    try:
        for ep_seg in range(start_ep, start_ep + n_epochs):
            t_ep = _time.time()
            if _tab is not None:
                _tab.epoca(ep_seg + 1, start_ep + n_epochs)
            log_event(_logdir, "INICIO epoca %d/%d segment=%s" % (ep_seg + 1, start_ep + n_epochs, _seg_label))
            model.train(); tl = 0.0; nb = 0
            _last_upd = t_ep; _last_hb = t_ep
            for ts, tgts in tr_l:
                opt.zero_grad()
                loss = batch_loss(ts, tgts, model, bce, flat_anc, device)
                loss.backward()
                opt.step(); tl += loss.item(); nb += 1
                now = _time.time()
                if _tab is not None:
                    _tab.update(nb, tl / max(nb, 1) if nb else None, {
                        "train": ("%.4f" % hist["train"][-1]) if hist["train"] else "--",
                        "val": ("%.4f" % hist["val"][-1]) if hist["val"] else "--",
                        "best": ("%.4f" % best) if best != float("inf") else "--",
                        "lr": "%.0e" % opt.param_groups[0]["lr"],
                        "paciencia": "%d/%d" % (wait, a.patience)})
                elif _tty and (now - _last_upd >= 3 or nb >= n_tr_batches):
                    el = now - t_ep
                    pct = 100.0 * nb / max(n_tr_batches, 1)
                    eta = el / max(nb, 1) * max(n_tr_batches - nb, 0)
                    sys.stdout.write("\r[%s] seg=%s ep=%d/%d batch=%d/%d (%.0f%%) t=%ds ETA=%ds loss~%.4f(parcial)" % (
                        _dt.now().astimezone().strftime("%H:%M:%S"), _seg_label, ep_seg + 1,
                        start_ep + n_epochs, nb, n_tr_batches, pct, el, eta, tl / max(nb, 1)))
                    sys.stdout.flush()
                    _last_upd = now
                elif not _tty and (now - _last_hb >= 60):
                    log_event(_logdir, "LATIDO epoca %d/%d batch=%d/%d loss~%.4f(parcial)" % (
                        ep_seg + 1, start_ep + n_epochs, nb, n_tr_batches, tl / max(nb, 1)))
                    _last_hb = now
            if _tab is None and _tty:
                sys.stdout.write("\n"); sys.stdout.flush()
            # val loss
            model.eval(); vl = 0.0; nvb = 0
            with torch.no_grad():
                for ts, tgts in va_l:
                    vl += batch_loss(ts, tgts, model, bce, flat_anc, device).item(); nvb += 1
            vl /= max(nvb, 1)
            if sched is not None:
                sched.step(vl)
            hist["train"].append(tl / max(nb, 1)); hist["val"].append(vl)
            dur = _time.time() - t_ep
            lr_now = opt.param_groups[0]["lr"]
            print("epoch=%d train=%.4f val=%.4f" % (ep_seg + 1, tl / max(nb, 1), vl), flush=True)
            if _tab is not None:
                sys.stdout.write("\n")
                sys.stdout.flush()
            if _tab is not None:
                _tab.resumen_epoca(ep_seg + 1, start_ep + n_epochs, tl / max(nb, 1), vl,
                                   best, "%.0e" % lr_now, wait, a.patience, dur)
            if mode == "segmento-1":
                if vl < best:
                    best, wait = vl, 0
                    torch.save({"model_state_dict": model.state_dict(), "epoch": ep_seg, "val_loss": vl,
                                "cfg": cfg}, ckpt if not a.smoke else ckpt.replace(".pth", "_smoke.pth"))
                else:
                    wait += 1
                    if wait >= a.patience and not a.smoke:
                        log_event(_logdir, "early stopping en epoca-seg %d" % (ep_seg + 1)); break
            else:
                state = {"model_state_dict": model.state_dict(), "optimizer_state_dict": opt.state_dict(),
                         "scheduler_state_dict": sched.state_dict(), "epoch": ep_seg, "best": best if vl >= best else vl,
                         "wait": 0 if vl < best else wait + 1, "rng": rng_states(), "cfg": cfg,
                         "history": hist, "provenance": provenance}
                if vl < best:
                    best, wait = vl, 0
                    state["best"], state["wait"] = best, wait
                    save_atomic(state, os.path.join(exp_dir, "best.pth"))
                    ckpt_mark = "best.pth"
                else:
                    wait += 1
                    state["wait"] = wait
                    ckpt_mark = "last.pth"
                    if wait >= a.patience and not a.smoke:
                        save_atomic(state, os.path.join(exp_dir, "last.pth"))
                        append_log(exp_dir, ep_seg, hist, dur, {"lr": lr_now, "best_val": best,
                                   "ckpt": ["last.pth", ckpt_mark]})
                        log_event(_logdir, "early stopping en epoca-seg %d" % (ep_seg + 1)); break
                save_atomic(state, os.path.join(exp_dir, "last.pth"))
                append_log(exp_dir, ep_seg, hist, dur, {"lr": lr_now, "best_val": best,
                           "ckpt": ["last.pth", ckpt_mark]})
                log_event(_logdir, "segment=%s | epoch=%d/%d | train_loss=%.4f | val_loss=%.4f | duration=%s | best_val=%.4f | checkpoint=%s | saved=OK" % (
                    _seg_label, ep_seg + 1, start_ep + n_epochs,
                    tl / max(nb, 1), vl, "%dm%ds" % (int(dur // 60), int(dur % 60)), best, ckpt_mark))
    except KeyboardInterrupt:
        log_event(_logdir, "INTERRUPCION solicitada (Ctrl+C): se conserva last.pth de la ultima epoca completa; la epoca actual NO cuenta como terminada")
        print("Para reanudar: --resume %s --epochs %d --batch %d --exp-dir %s" % (
            os.path.join(exp_dir, "last.pth") if mode != "segmento-1" else ckpt, a.epochs, a.batch, exp_dir))
        raise SystemExit(130)
    finally:
        try:
            if _tab is not None:
                _tab.cerrar()
        except Exception:
            pass
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        if not a.smoke:
            release_lock(exp_dir)
    if mode == "segmento-1":
        if not a.smoke:
            json.dump({"cfg": cfg, "history": hist, "best_val": best},
                      open(os.path.join(exp_dir, "detector_facial_wider_v1_cfg.json"), "w"), indent=2)
        print("OK", ckpt)
    else:
        json.dump({"cfg": cfg, "provenance": provenance, "best_val": best, "history": hist},
                  open(os.path.join(exp_dir, "config.json"), "w"), indent=2)
        print("OK", exp_dir, "best=%.4f" % best)

if __name__ == "__main__":
    main()
