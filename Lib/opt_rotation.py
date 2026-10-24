# -*- coding: utf-8 -*-
import math

_applied = False
_orig_transform = None


def apply(verbose=True):
    global _applied, _orig_transform
    if _applied:
        return
    from live2d.v2.core.deformer.roation_deformer import RotationDeformer
    from live2d.v2.core.util import UtMath

    DEG_TO_RAD = UtMath.DEG_TO_RAD
    sin, cos = math.sin, math.cos
    _orig_transform = RotationDeformer.transformPoints

    def transformPoints_fast(self, modelContext, rotationContext, srcPoints, dstPoints,
                             numPoint, ptOffset, ptStep):
        d = rotationContext.getDeformer()
        if d is not self and d != self:
            raise RuntimeError("context not match")

        affine = rotationContext.transformedAffine
        if affine is None:
            affine = rotationContext.interpolatedAffine

        rot = affine.rotationDeg
        if rot:
            a = DEG_TO_RAD * rot
            s, c = sin(a), cos(a)
        else:
            s, c = 0.0, 1.0

        ts = rotationContext.getTotalScale()
        if affine.reflectX:
            scaleX, shearY = -c * ts, -s * ts
        else:
            scaleX, shearY = c * ts, s * ts
        if affine.reflectY:
            shearX, scaleY = s * ts, -c * ts
        else:
            shearX, scaleY = -s * ts, c * ts
        ox, oy = affine.originX, affine.originY

        if numPoint == 1 and ptOffset == 0 and ptStep == 2:
            x = srcPoints[0]
            y = srcPoints[1]
            dstPoints[0] = scaleX * x + shearX * y + ox
            dstPoints[1] = shearY * x + scaleY * y + oy
            return

        total = numPoint * ptStep
        sp, dp = srcPoints, dstPoints
        for i in range(ptOffset, total, ptStep):
            x = sp[i]
            y = sp[i + 1]
            dp[i] = scaleX * x + shearX * y + ox
            dp[i + 1] = shearY * x + scaleY * y + oy

    RotationDeformer.transformPoints = transformPoints_fast
    _applied = True
    if verbose:
        print("[opt_rotation] transformPoints 快速路径已应用")


def restore():
    global _applied
    if _applied and _orig_transform is not None:
        from live2d.v2.core.deformer.roation_deformer import RotationDeformer
        RotationDeformer.transformPoints = _orig_transform
        _applied = False
