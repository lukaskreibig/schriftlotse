from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from schriftlotse.domain import LineResult

NS = "http://schema.primaresearch.org/PAGE/gts/pagecontent/2019-07-15"
ET.register_namespace("", NS)


def _points(bbox: tuple[int, int, int, int]) -> str:
    x1, y1, x2, y2 = bbox
    return f"{x1},{y1} {x2},{y1} {x2},{y2} {x1},{y2}"


def write_segmentation(
    output: Path,
    image_path: Path,
    width: int,
    height: int,
    boxes: list[tuple[int, int, int, int]],
) -> None:
    root = ET.Element(f"{{{NS}}}PcGts")
    metadata = ET.SubElement(root, f"{{{NS}}}Metadata")
    ET.SubElement(metadata, f"{{{NS}}}Creator").text = "SchriftLotse"
    page = ET.SubElement(
        root,
        f"{{{NS}}}Page",
        imageFilename=str(image_path.resolve()),
        imageWidth=str(width),
        imageHeight=str(height),
    )
    region = ET.SubElement(page, f"{{{NS}}}TextRegion", id="region_1")
    ET.SubElement(region, f"{{{NS}}}Coords", points=f"0,0 {width},0 {width},{height} 0,{height}")
    for index, bbox in enumerate(boxes):
        line = ET.SubElement(region, f"{{{NS}}}TextLine", id=f"line_{index:04d}")
        ET.SubElement(line, f"{{{NS}}}Coords", points=_points(bbox))
        x1, _, x2, y2 = bbox
        ET.SubElement(line, f"{{{NS}}}Baseline", points=f"{x1},{y2 - 2} {x2},{y2 - 2}")
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)


def parse_recognized(path: Path, model: str, variant: str) -> list[LineResult]:
    tree = ET.parse(path)
    lines: list[LineResult] = []
    for index, element in enumerate(tree.findall(f".//{{{NS}}}TextLine")):
        coords = element.find(f"{{{NS}}}Coords")
        points = [] if coords is None else coords.attrib.get("points", "").split()
        xy = [tuple(map(int, point.split(","))) for point in points if "," in point]
        bbox = (
            min((point[0] for point in xy), default=0),
            min((point[1] for point in xy), default=0),
            max((point[0] for point in xy), default=0),
            max((point[1] for point in xy), default=0),
        )
        unicode_node = element.find(f".//{{{NS}}}Unicode")
        text = (
            "" if unicode_node is None or unicode_node.text is None else unicode_node.text.strip()
        )
        confidence = float(element.attrib.get("conf", "0.75"))
        lines.append(
            LineResult(
                id=f"party-{index}",
                text=text,
                bbox=bbox,
                confidence=max(0.0, min(confidence, 1.0)),
                model=model,
                variant=variant,
            )
        )
    return [line for line in lines if line.text]
