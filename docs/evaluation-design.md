# StylePilot evaluation design

Status: M3.5 baseline specification. This document defines what StylePilot
measures before the learned planner or a larger Lightroom parameter surface is
treated as a product capability.

## Principles

StylePilot does not have a pixel-aligned target edit for an arbitrary source
RAW. A photographer's published references differ in subject, lighting,
camera, and composition, so single-reference pixel error is not ground truth.
The evaluation therefore separates four questions:

1. **Actuation:** did Lightroom apply and restore the requested setting?
2. **Eligibility:** should this source photo be edited toward this profile?
3. **Safety:** did the rendered result preserve usable tonal and structural
   information?
4. **Style movement:** did an eligible, safe result move toward the target
   photographer's content-conditioned feature distribution?

No aggregate style score may hide a failed actuation or safety gate. VLM
output is used for semantic conditioning, not as the sole visual-quality
judge.

## Evaluation layers

### E0: Lightroom actuator contract

The first executable benchmark is a single-parameter probe. For an explicitly
approved target value it must:

1. render and measure the selected source photo;
2. create a probe virtual copy;
3. create a recovery snapshot;
4. apply one allowlisted absolute Develop value;
5. read the value back from Lightroom;
6. render and measure the changed virtual copy;
7. restore the recovery snapshot;
8. read and render the restored state again.

The v1 probe restores but deliberately does not delete its virtual copy. The
copy remains visible in Lightroom for audit and can be removed manually after
review. Automated destructive cleanup is outside this benchmark's authority.

The report records requested, applied, and restored values; readback errors;
signed rendered-feature deltas; and absolute post-restore metric drift. It does
not invent a universal rendered-drift pass threshold before real Lightroom
runs establish export noise and per-parameter sensitivity.

Initial actuator metrics:

- apply readback absolute error;
- restore readback absolute error;
- apply and restore success rate;
- rendered delta per parameter and photo;
- post-restore rendered metric drift;
- end-to-end latency and failure reason (in a later batch runner).

#### Batch actuator calibration

The batch calibrator turns the single-point probe into a bounded experiment:

1. select 1–20 representative original photos in Lightroom Classic;
2. define explicit parameter/value sweeps in a versioned JSON manifest;
3. review the full scope once in Lightroom, including file, copy, sample, and
   render counts plus every parameter value;
4. repeat source renders to estimate baseline repeatability;
5. create one virtual copy per source, measure each declared point, and restore
   its recovery snapshot before continuing.

Run the checked-in smoke manifest with:

```bash
uv run stylepilot lightroom calibrate \
  --manifest examples/actuator-calibration.json
```

Reports are written to `.stylepilot/evaluations/calibrations/<run-id>.json`.
They keep actuator conformance, rendered response, baseline repeatability, and
restoration integrity separate rather than hiding them behind one score. This
evaluates reliable Lightroom control, not subjective photographer-style match;
the later style evaluator can consume these calibrated response curves.

### E1: semantic eligibility

Eligibility is evaluated independently from edit quality on labelled
source/profile pairs:

- false-accept rate for incompatible pairs;
- false-reject rate for compatible pairs;
- coverage and abstention rate;
- confidence calibration by scene group.

Splits must be grouped by capture series so near-duplicate frames never occur
in both calibration and evaluation data.

### E2: hard safety gates

An edit cannot pass merely by reducing style distance. Initial hard gates are:

- highlight and shadow clipping limits;
- successful virtual-copy-only execution and recovery;
- no material regression in the calibrated style distance;
- bounded adjustment magnitude.

Region-specific skin plausibility, structural preservation, local-detail loss,
and channel clipping join this layer when Style Profile v2 adds semantic masks
and richer rendered features.

### E3: style distribution and movement

A style is a distribution across curated reference images, not one target
image. For region `r`, the calibrated distance is conceptually:

```text
d_r(x, style) = distance(features_r(x), robust_distribution_r(style))
D_style       = sum_r(weight_r * d_r)
StyleGain     = (D_before - D_after) / max(D_before, epsilon)
```

The current global Lab profile is only the first feature family. Style Profile
v2 should add luminance quantiles, local contrast, Lab/LCh distributions, and
separate skin, sky, vegetation, water, subject, and background measurements.
Dispersion from the references normalizes each feature so stable stylistic
choices matter more than naturally variable ones.

The distance metric itself must pass leave-one-reference-out validation:

1. remove one image from a photographer's references;
2. build the profile from the remaining images;
3. rank the held-out image against all photographer profiles;
4. repeat for every reference.

Report top-1 style retrieval, rank, same-style distance, and cross-style
distance. Deliberately damaged variants (clipping, crushed blacks,
desaturation, extreme saturation, hue shifts, blur) must not receive a better
score simply by matching a global average.

### E4: product stability and efficiency

Once batch evaluation exists, record repeated-run parameter variance,
successful-edit rate, regression rate, Lightroom render count, wall time,
model calls, and estimated model cost. These are reported beside the quality
metrics and are not folded into style distance.

## Datasets

Three datasets serve different purposes:

- **Actuator calibration set:** a small, user-owned RAW set spanning portrait,
  landscape, high/low exposure, mixed white balance, and high ISO. It is safe
  to modify through temporary virtual copies.
- **Style reference set:** curated rendered works per photographer, stored
  locally when licensing does not permit redistribution. Photographer and
  capture-series IDs are required for grouped splits.
- **Edit challenge set:** user-owned RAW files paired with compatible and
  incompatible target profiles. It contains easy cases, boundary cases, and
  explicit abstention cases.

Dataset manifests contain paths and labels, never API keys or copyrighted
image bytes. Generated previews and reports live below `.stylepilot/` and stay
Git-ignored by default.

## Report policy

Per-image reports preserve raw component metrics. Aggregate reports use median,
interquartile range, tail regressions, and group breakdowns instead of only a
mean. Thresholds are versioned and derived from calibration data. A future
composite product score may be displayed only after the actuator and safety
gates pass, and its component values must remain visible.

## Implementation order

1. ~~single-point actuator probe for the existing eleven guarded parameters;~~
2. ~~repeated real-Lightroom calibration runs and rendered-noise measurement;~~
3. activate basic tone controls only where a target feature exists;
4. add typed white-balance and HSL settings to Python, TypeScript, and Lua
   guardrails in lockstep;
5. build the leave-one-reference-out style-distance benchmark;
6. add semantic region features and then a constrained closed-loop planner.

The first real batch smoke run completed on 2026-08-18 with one selected photo,
two baseline repeats, three `Contrast2012` points, and nine Lightroom renders.
All three applied/restored readbacks had zero error; repeated baseline and all
post-restore rendered metric drifts were also zero. This is a connectivity and
recovery result, not yet a statistically representative parameter benchmark.
