from enum import Enum


class PlantType(str, Enum):
    TOMATO = "tomato"
    EGGPLANT = "eggplant"
    PEPPER = "pepper"
    RAPESEED = "rapeseed"


YOLO_INI_FILES = {
    PlantType.TOMATO: "20260411-Tomato-006.ini",
    PlantType.EGGPLANT: "20261020-eggplant-class.ini",
    PlantType.PEPPER: "20260720-lajiao.ini",
    PlantType.RAPESEED: "903_YOLO26.ini",
}


def yolo_ini_for(plant_type: PlantType | str | None) -> str | None:
    """Resolve only server-approved filenames; legacy tasks have no selection."""
    return YOLO_INI_FILES[PlantType(plant_type)] if plant_type is not None else None
