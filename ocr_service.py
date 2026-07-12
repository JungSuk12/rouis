import gc
import os
import threading
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps


# =========================================================
# 저메모리 CPU 설정
# =========================================================

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")


# =========================================================
# OCR 설정
# =========================================================

# 일부러 메모리를 줄인 설정
OCR_IMAGE_SIDE = 256
OCR_JPEG_QUALITY = 70

OCR_MODEL_DIR = os.environ.get(
    "OCR_MODEL_DIR",
    "/var/data/easyocr-models",
).strip()


# =========================================================
# OCR 상태
# =========================================================

_ocr_reader = None
_ocr_reader_lock = threading.Lock()
_ocr_run_lock = threading.Lock()


def configure_torch_for_low_memory() -> None:
    try:
        import torch

        torch.set_num_threads(1)

        try:
            torch.set_num_interop_threads(1)
        except RuntimeError:
            pass

    except Exception as error:
        print(
            "[OCR] torch low-memory setting skipped: "
            f"{type(error).__name__}: {error}",
            flush=True,
        )


def get_ocr_reader():
    global _ocr_reader

    if _ocr_reader is not None:
        return _ocr_reader

    with _ocr_reader_lock:
        if _ocr_reader is not None:
            return _ocr_reader

        print(
            "[OCR] EasyOCR Reader loading...",
            flush=True,
        )

        import easyocr

        configure_torch_for_low_memory()

        Path(OCR_MODEL_DIR).mkdir(
            parents=True,
            exist_ok=True,
        )

        _ocr_reader = easyocr.Reader(
            ["ko", "en"],
            gpu=False,
            quantize=True,
            model_storage_directory=OCR_MODEL_DIR,
            download_enabled=True,
            detector=True,
            recognizer=True,
            verbose=False,
        )

        print(
            "[OCR] EasyOCR Reader ready",
            flush=True,
        )

    return _ocr_reader


def create_ocr_image(
    source_image_path: str,
) -> str:
    source_path = Path(source_image_path)

    ocr_path = (
        source_path.parent
        / f"{source_path.stem}_ocr.jpg"
    )

    ocr_image = None
    transposed_image = None
    grayscale_image = None

    try:
        with Image.open(
            source_image_path
        ) as source_image:
            transposed_image = (
                ImageOps.exif_transpose(
                    source_image
                )
            )

            grayscale_image = (
                transposed_image.convert("L")
            )

            if (
                max(grayscale_image.size)
                > OCR_IMAGE_SIDE
            ):
                grayscale_image.thumbnail(
                    (
                        OCR_IMAGE_SIDE,
                        OCR_IMAGE_SIDE,
                    ),
                    Image.Resampling.BILINEAR,
                    reducing_gap=2.0,
                )

            ocr_image = grayscale_image.copy()

        ocr_image.save(
            ocr_path,
            format="JPEG",
            quality=OCR_JPEG_QUALITY,
            optimize=False,
            progressive=False,
        )

        print(
            "[OCR] temporary image prepared "
            f"size={ocr_image.width}x{ocr_image.height}",
            flush=True,
        )

        return str(ocr_path)

    finally:
        if ocr_image is not None:
            ocr_image.close()

        if grayscale_image is not None:
            grayscale_image.close()

        if transposed_image is not None:
            try:
                transposed_image.close()
            except Exception:
                pass


def extract_text_from_image(
    image_path: str,
) -> str:
    ocr_image_path: Optional[str] = None
    results = None

    with _ocr_run_lock:
        try:
            ocr_image_path = create_ocr_image(
                image_path
            )

            reader = get_ocr_reader()

            print(
                "[OCR] readtext started "
                f"path={ocr_image_path}",
                flush=True,
            )

            results = reader.readtext(
                ocr_image_path,
                detail=0,
                paragraph=False,
                decoder="greedy",
                beamWidth=1,
                batch_size=1,
                workers=0,
                canvas_size=OCR_IMAGE_SIDE,
                mag_ratio=1.0,
                min_size=8,
                rotation_info=None,
            )

            texts = [
                str(item).strip()
                for item in results
                if str(item).strip()
            ]

            print(
                "[OCR] readtext finished "
                f"count={len(texts)}",
                flush=True,
            )

            return "\n".join(texts)

        finally:
            results = None

            if ocr_image_path:
                try:
                    Path(
                        ocr_image_path
                    ).unlink(
                        missing_ok=True
                    )
                except OSError as error:
                    print(
                        "[OCR] temporary image delete failed: "
                        f"{error}",
                        flush=True,
                    )

            gc.collect()


def is_ocr_loaded() -> bool:
    return _ocr_reader is not None