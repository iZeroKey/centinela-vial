#!/usr/bin/env python3
"""
Centinela Control — Arquitectura e inferencia del detector facial.

Single-stage multi-escala con anclas y NMS propio para detección de rostros.
"""

import torch
import torch.nn as nn

IN_W, IN_H = 640, 480
STRIDES = [8, 16, 32]
ANCHORS = {8: [16, 32, 64], 16: [32, 64, 128], 32: [64, 128, 256]}


def build_anchors():
    """Construye el diccionario de anclas por stride en formato (A, H, W, 4)."""
    anc = {}
    for st in STRIDES:
        gw, gh = IN_W // st, IN_H // st
        ys, xs = torch.meshgrid(torch.arange(gh), torch.arange(gw), indexing="ij")
        cx = (xs.float() + 0.5) * st
        cy = (ys.float() + 0.5) * st
        per = []
        for s in ANCHORS[st]:
            per.append(
                torch.stack(
                    [cx, cy, torch.full_like(cx, float(s)), torch.full_like(cy, float(s))],
                    -1,
                )
            )
        anc[st] = torch.stack(per, 0)
    return anc


ANCH = build_anchors()
_ANCH_FLAT_CACHE = {}


def _get_anchors_flat(stride, device):
    """Cachea los tensores de anclas planos en el dispositivo destino."""
    key = (stride, str(device))
    cached = _ANCH_FLAT_CACHE.get(key)
    if cached is None:
        a = len(ANCHORS[stride])
        cached = ANCH[stride].permute(1, 2, 0, 3).to(device).reshape(-1, a, 4)
        _ANCH_FLAT_CACHE[key] = cached
    return cached


def box_iou(a, b):
    """Calcula IoU entre cajas a:(N,4) y b:(M,4) en formato xyxy."""
    ax1, ay1, ax2, ay2 = a[:, 0:1], a[:, 1:2], a[:, 2:3], a[:, 3:4]
    bx1, by1, bx2, by2 = b[:, 0], b[:, 1], b[:, 2], b[:, 3]
    ix1 = torch.maximum(ax1, bx1)
    iy1 = torch.maximum(ay1, by1)
    ix2 = torch.minimum(ax2, bx2)
    iy2 = torch.minimum(ay2, by2)
    iw = (ix2 - ix1).clamp(min=0)
    ih = (iy2 - iy1).clamp(min=0)
    inter = iw * ih
    ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / ua.clamp(min=1e-6)


class Backbone(nn.Module):
    """Extractor convolucional multi-escala del detector facial."""

    def __init__(self):
        super().__init__()

        def blk(ci, co, s):
            return nn.Sequential(
                nn.Conv2d(ci, co, 3, stride=s, padding=1),
                nn.BatchNorm2d(co),
                nn.ReLU(inplace=True),
                nn.Conv2d(co, co, 3, padding=1),
                nn.BatchNorm2d(co),
                nn.ReLU(inplace=True),
            )

        self.c1 = blk(3, 32, 2)
        self.c2 = blk(32, 64, 2)
        self.c3 = blk(64, 128, 2)
        self.c4 = blk(128, 256, 2)
        self.c5 = blk(256, 256, 2)

    def forward(self, x):
        x = self.c1(x)
        x = self.c2(x)
        f8 = self.c3(x)
        f16 = self.c4(f8)
        f32 = self.c5(f16)
        return {8: f8, 16: f16, 32: f32}


class Detector(nn.Module):
    """Cabezas de objetividad y regresión de cajas sobre Backbone."""

    def __init__(self, n_anc=3):
        super().__init__()
        self.back = Backbone()
        self.obj = nn.ModuleDict(
            {str(s): nn.Conv2d({8: 128, 16: 256, 32: 256}[s], n_anc, 1) for s in STRIDES}
        )
        self.box = nn.ModuleDict(
            {str(s): nn.Conv2d({8: 128, 16: 256, 32: 256}[s], n_anc * 4, 1) for s in STRIDES}
        )
        for m in (self.obj, self.box):
            for c in m.values():
                nn.init.normal_(c.weight, std=0.01)
                nn.init.zeros_(c.bias)

    def forward(self, x):
        f = self.back(x)
        out = {}
        for s in STRIDES:
            k = str(s)
            out[s] = (self.obj[k](f[s]), self.box[k](f[s]))
        return out


def decode(anc_cxcywh, deltas):
    """Decodifica deltas respecto a las anclas a coordenadas xyxy."""
    ax, ay, aw, ah = anc_cxcywh.unbind(-1)
    px = deltas[..., 0] * aw + ax
    py = deltas[..., 1] * ah + ay
    pw = torch.exp(deltas[..., 2].clamp(max=6)) * aw
    ph = torch.exp(deltas[..., 3].clamp(max=6)) * ah
    return torch.stack([px - pw / 2, py - ph / 2, px + pw / 2, py + ph / 2], -1)


def nms(boxes, scores, thr=0.5, topk=500):
    """Supresión no máxima sobre cajas xyxy ordenadas por score."""
    if boxes.numel() == 0:
        return torch.zeros(0, dtype=torch.long)
    order = scores.argsort(descending=True)[: topk * 2]
    boxes, scores = boxes[order], scores[order]
    keep = []
    while order.numel():
        keep.append(order[0].item())
        if order.numel() == 1:
            break
        iou = box_iou(boxes[0:1], boxes[1:]).squeeze(0)
        mascara = iou <= thr
        order = order[1:][mascara]
        boxes = boxes[1:][mascara]
    return torch.tensor(keep[:topk], dtype=torch.long)


@torch.inference_mode()
def infer(model, img_t, thr=0.3, nms_thr=0.5):
    """Ejecuta inferencia sobre un tensor (3, H, W) y devuelve (cajas_xyxy, scores)."""
    model.eval()
    device = next(model.parameters()).device
    outs = model(img_t.unsqueeze(0).to(device))
    all_b, all_s = [], []
    for s in STRIDES:
        o, d = outs[s]
        a = len(ANCHORS[s])
        scores = torch.sigmoid(o[0]).permute(1, 2, 0).reshape(-1, a)
        delt = d[0].permute(1, 2, 0).reshape(-1, a, 4)
        anc = _get_anchors_flat(s, device)
        boxes = decode(anc, delt).reshape(-1, 4)
        scores = scores.reshape(-1)
        m = scores > 0.05
        all_b.append(boxes[m])
        all_s.append(scores[m])
    if not all_b:
        return torch.zeros(0, 4), torch.zeros(0)
    b = torch.cat(all_b)
    s_cat = torch.cat(all_s)
    b[:, 0::2].clamp_(0, IN_W)
    b[:, 1::2].clamp_(0, IN_H)
    k = nms(b, s_cat, nms_thr)
    m2 = s_cat[k] >= thr
    return b[k][m2].cpu(), s_cat[k][m2].cpu()
