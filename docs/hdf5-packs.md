# HDF5 doctor reading packs

The original patient HDF5 files are mounted **read only** into /packs. The app does not copy or expose H5 volumes to readers.

## Layout
One `patient_id.h5` per patient, with:
- `ncct` [D,512,512]
- `real_cect`, `syn_cect` [3,D,512,512], phase order AP/PVP/DP
- `liver_mask` [D,512,512]
- `slice_indices` [D]

Optional datasets (uncertainty, confidence, syn_mean_cect) are not shown during the blinded first two modules.

## Import
1. Start the container; confirm `/packs` points to the correct H5 folder.
2. Sign in as admin; click **从 /packs 导入 HDF5**.
3. For each H5 file, the backend picks a *single predetermined liver-containing z index*, the median index among non-empty liver-mask slices (or D//2 when unavailable).
4. Import creates three quality cases per patient per arm (AP/PVP/DP) and one diagnostic case per patient per arm (NCCT + AP/PVP/DP).
5. Click **生成盲法任务（试运行）** to distribute tasks.
6. Doctor image requests render one 512x512 PNG per phase directly from physical HU. Fixed abdominal window [-160,240] HU. No 3D volume exposed.

Import is idempotent for existing (patient, module, source, phase) rows. Changed H5 files require a versioned reimport strategy rather than silently changing active study data.

## Important: pilot limitations
- The current assignment generator does **not** enforce crossover washout or separation of paired Real/Virtual tasks. Do not use it unchanged for the definitive non-inferiority reader study.
- Default slice selection based on liver mask is **not lesion-targeted**. A diagnosis reader could see no target lesion. A gold-standard lesion coordinate / chosen-slice manifest should define z independently of Real/Virtual appearance for formal diagnostic experiments.
- Reference diagnoses are NOT invented by the importer. Populate validated lesion labels and reference standards separately.
- The image endpoint renders PNG at fixed HU window; in-browser zoom is available, but dynamic HU windowing is not yet implemented.
- Only selected 2D slices are assessed. No full volume diagnostic conclusion is warranted.
- Check institutional privacy and security requirements before external access.
