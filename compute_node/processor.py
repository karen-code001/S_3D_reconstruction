from pathlib import Path


async def run_reconstruction(input_path: Path, output_path: Path) -> None:
    """Placeholder for the real reconstruction pipeline.

    Replace this function with the GPU reconstruction implementation. The
    surrounding API only depends on the input and output paths.
    """
    output_path.write_text(
        f"placeholder result for {input_path.name}\n",
        encoding="utf-8",
    )
