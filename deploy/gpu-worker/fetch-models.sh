set -euo pipefail

models_dir="${1:-/opt/ai2-models}"
base="${models_dir}/projects"

fetch() {
    local relative_path="$1"
    local url="$2"
    local destination="${base}/${relative_path}/best.ckpt"
    local temporary="${destination}.download"

    mkdir -p "$(dirname "${destination}")"
    if [ -s "${destination}" ]; then
        return
    fi
    curl --fail --location --retry 3 --output "${temporary}" "${url}"
    test -s "${temporary}"
    mv "${temporary}" "${destination}"
}

fetch "20260626_num_historical/data_20250521_model_20250530_satlaspretrain_unfreeze4_13_1hist" "https://storage.googleapis.com/ai2-rslearn-projects-data/projects/20260626_num_historical/data_20250521_model_20250530_satlaspretrain_unfreeze4_13_1hist/best.ckpt"
fetch "2026_09_17_vessel_attribute_l1/sentinel1_l1" "https://storage.googleapis.com/ai2-rslearn-projects-data/projects/2026_09_17_vessel_attribute_l1/sentinel1_l1/best.ckpt"
fetch "sentinel2_vessels/data_20250213_02_all_bands" "https://storage.googleapis.com/ai2-rslearn-projects-data/projects/sentinel2_vessels/data_20250213_02_all_bands/best.ckpt"
fetch "2026_09_17_vessel_attribute_l1_01/sentinel2_l1" "https://storage.googleapis.com/ai2-rslearn-projects-data/projects/2026_09_17_vessel_attribute_l1_01/sentinel2_l1/best.ckpt"

find "${base}" -name best.ckpt -type f -size +0c -print
