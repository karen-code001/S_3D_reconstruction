from pathlib import Path

from common.plants import PlantType, yolo_ini_for


async def run_reconstruction(
    input_path: Path,
    output_path: Path,
    *,
    plant_type: PlantType | None = None,
    yolo_ini: str | None = None,
) -> None:
    """Placeholder for the real reconstruction pipeline.

    Replace this function with the GPU reconstruction implementation. The
    yolo_ini is the approved config filename for plant_type. The real pipeline
    must resolve it inside its own YOLO config directory and load that file.
    This placeholder does not run YOLO or load the ini file.
    """
    if yolo_ini != yolo_ini_for(plant_type):
        raise ValueError("yolo_ini does not match plant_type")
    selection = ""
    if plant_type is not None:
        selection = f"plant_type={PlantType(plant_type).value}\nyolo_ini={yolo_ini}\n"
    output_path.write_text(
        f"placeholder result for {input_path.name}\n{selection}",
        encoding="utf-8",
    )
