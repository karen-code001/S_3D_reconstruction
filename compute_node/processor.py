from pathlib import Path


async def run_reconstruction(
    input_path: Path,
    output_path: Path,
    *,
    plant_type: str | None = None,
    yolo_ini: str | None = None,
    segment: bool | None = None,
) -> None:
    """Placeholder for the real reconstruction pipeline.

    Replace this function with the GPU reconstruction implementation. yolo_ini and
    segment are the crop settings the control plane recorded for this task. The real
    pipeline must resolve yolo_ini inside its own YOLO config directory and load that
    file. This placeholder does not run YOLO or load the ini file.
    """
    selection = ""
    if plant_type is not None:
        selection = f"plant_type={plant_type}\nyolo_ini={yolo_ini}\nsegment={segment}\n"
    output_path.write_text(
        f"placeholder result for {input_path.name}\n{selection}",
        encoding="utf-8",
    )
