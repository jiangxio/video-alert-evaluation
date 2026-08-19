"""od-dataset-manager 纯函数单元测试：dHash / Hamming / IoU 计算逻辑。

这些是 qc（模糊检测）与 cross-qc（跨版本相似图）的核心计算，纯函数易测。
"""
from pathlib import Path

from PIL import Image, ImageDraw


class TestDhash:
    def test_same_image_same_hash(self, od_module, tmp_path):
        p = tmp_path / "a.png"
        _draw_pattern(p)
        assert od_module._dhash(str(p)) == od_module._dhash(str(p))

    def test_different_images_different_hash(self, od_module, tmp_path):
        # 一张有图案（dHash 非 0），一张全黑（无像素差异，dHash=0）
        a = tmp_path / "a.png"
        _draw_pattern(a)
        b = tmp_path / "b.png"
        Image.new("L", (64, 64), 0).save(b)
        ha, hb = od_module._dhash(str(a)), od_module._dhash(str(b))
        assert ha is not None
        assert ha != hb

    def test_non_image_returns_none(self, od_module, tmp_path):
        p = tmp_path / "x.txt"
        p.write_text("not an image")
        assert od_module._dhash(str(p)) is None


class TestHamming:
    def test_identical_zero(self, od_module):
        assert od_module._hamming(0b1010, 0b1010) == 0

    def test_all_bits_differ(self, od_module):
        # 1010 ^ 0101 = 1111 → 4 位不同
        assert od_module._hamming(0b1010, 0b0101) == 4

    def test_partial(self, od_module):
        assert od_module._hamming(0b1100, 0b1001) == 2


class TestIou:
    def test_identical_boxes(self, od_module):
        assert od_module._compute_iou([(0, 0), (2, 2)], [(0, 0), (2, 2)]) == 1.0

    def test_disjoint_zero(self, od_module):
        assert od_module._compute_iou([(0, 0), (1, 1)], [(5, 5), (6, 6)]) == 0.0

    def test_partial_overlap(self, od_module):
        # 两框部分重叠，IoU 在 (0,1)
        iou = od_module._compute_iou([(0, 0), (2, 2)], [(1, 1), (3, 3)])
        assert 0.0 < iou < 1.0


def _draw_pattern(path: Path):
    """画一张有明显纹理的图（黑底 + 白色矩形），保证 dHash 非 0。"""
    img = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(img).rectangle([10, 10, 40, 40], fill=255)
    img.save(path)
