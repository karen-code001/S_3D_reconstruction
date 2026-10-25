import re
from dataclasses import dataclass

# Crops are managed at runtime in the control plane (plants table, /ui/plants.html).
# Keys reach the compute node and file names, so both are kept to a safe character set.
PLANT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,19}$")
# psn_pipeline_cuda resolves --ini by name inside its own config directory; never accept a path.
YOLO_INI_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.ini$")


@dataclass(frozen=True)
class PlantSeed:
    key: str
    name_zh: str
    name_en: str
    yolo_ini: str
    segment: bool = True
    enabled: bool = True


# Written to an empty plants table on first start; edit crops on the admin page afterwards.
DEFAULT_PLANTS = (
    PlantSeed("tomato_new", "番茄", "Tomato", "20261020-Tomato-CEN-fewshot.ini"),
    PlantSeed("eggplant", "茄子", "Eggplant", "20261024-CENCLASS-922.ini"),
    PlantSeed("pepper", "辣椒", "Pepper", "20260720-lajiao.ini"),
    PlantSeed("rapeseed", "油菜", "Rapeseed", "903_YOLO26.ini"),
    PlantSeed("ginkgo", "银杏", "Ginkgo", "20260323-yinxing.ini"),
    # Reconstruction only; the compute node skips YOLO but still requires an --ini value.
    PlantSeed("other", "其他植物", "Other plant", "903_YOLO26.ini", segment=False),
    PlantSeed("tomato", "番茄（旧）", "Tomato (old)", "20260411-Tomato-006.ini", enabled=False),
)

# Tasks created before the plants table stored no ini; resolve them as they ran back then.
LEGACY_YOLO_INI = {plant.key: plant.yolo_ini for plant in DEFAULT_PLANTS}
LEGACY_RECONSTRUCTION_ONLY = {plant.key for plant in DEFAULT_PLANTS if not plant.segment}


def validate_plant_key(value: str) -> str:
    if not PLANT_KEY_PATTERN.fullmatch(value):
        raise ValueError("plant key must be 1-20 lowercase letters, digits or underscores, starting with a letter")
    return value


def validate_yolo_ini(value: str) -> str:
    if not YOLO_INI_PATTERN.fullmatch(value):
        raise ValueError("yolo_ini must be a plain .ini file name without directories")
    return value


def yolo_ini_for(plant_type: str | None) -> str | None:
    """Ini used by a legacy task that has no snapshot of its own."""
    return LEGACY_YOLO_INI.get(plant_type) if plant_type else None
