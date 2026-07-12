import gc
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageEnhance, ImageOps


# =========================================================
# 저메모리 CPU 설정
# =========================================================

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("TORCH_NUM_THREADS", "1")


# =========================================================
# OCR 설정
# =========================================================

OCR_IMAGE_SIDE = int(
    os.environ.get(
        "OCR_IMAGE_SIDE",
        "512",
    )
)

OCR_CONTRAST = float(
    os.environ.get(
        "OCR_CONTRAST",
        "1.20",
    )
)

OCR_MODEL_DIR = os.environ.get(
    "OCR_MODEL_DIR",
    "easyocr-models",
).strip()


# =========================================================
# OCR 상태
# =========================================================

_ocr_reader = None
_ocr_reader_lock = threading.Lock()
_ocr_run_lock = threading.Lock()


# =========================================================
# PyTorch 저메모리 설정
# =========================================================

def configure_torch_for_low_memory() -> None:
    try:
        import torch

        torch.set_num_threads(1)

        try:
            torch.set_num_interop_threads(1)

        except RuntimeError:
            pass

        print(
            "[OCR] torch low-memory configuration applied",
            flush=True,
        )

    except Exception as error:
        print(
            "[OCR] torch low-memory setting skipped: "
            f"{type(error).__name__}: {error}",
            flush=True,
        )


# =========================================================
# OCR 모델 저장 경로 결정
# =========================================================

def resolve_ocr_model_directory() -> str:
    preferred_path = Path(
        OCR_MODEL_DIR
    )

    try:
        preferred_path.mkdir(
            parents=True,
            exist_ok=True,
        )

        test_file = (
            preferred_path
            / ".ocr_write_test"
        )

        test_file.write_text(
            "test",
            encoding="utf-8",
        )

        test_file.unlink(
            missing_ok=True
        )

        print(
            "[OCR] model directory ready: "
            f"{preferred_path}",
            flush=True,
        )

        return str(
            preferred_path
        )

    except Exception as error:
        fallback_path = Path(
            "easyocr-models"
        ).resolve()

        fallback_path.mkdir(
            parents=True,
            exist_ok=True,
        )

        print(
            "[OCR] preferred model directory unavailable: "
            f"{preferred_path} / "
            f"{type(error).__name__}: {error}",
            flush=True,
        )

        print(
            "[OCR] fallback model directory: "
            f"{fallback_path}",
            flush=True,
        )

        return str(
            fallback_path
        )


# =========================================================
# EasyOCR Reader 지연 로딩
# =========================================================

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

        print(
            "[OCR] torch configuration starting",
            flush=True,
        )

        configure_torch_for_low_memory()

        print(
            "[OCR] torch configuration finished",
            flush=True,
        )

        print(
            "[OCR] easyocr import starting",
            flush=True,
        )

        import easyocr

        print(
            "[OCR] easyocr import finished",
            flush=True,
        )

        print(
            "[OCR] model directory resolving",
            flush=True,
        )

        model_directory = (
            resolve_ocr_model_directory()
        )

        print(
            "[OCR] model directory resolved "
            f"path={model_directory}",
            flush=True,
        )

        print(
            "[OCR] Reader constructor starting",
            flush=True,
        )

        _ocr_reader = easyocr.Reader(
            ["ko", "en"],
            gpu=False,
            quantize=True,
            model_storage_directory=(
                model_directory
            ),
            download_enabled=False,
            detector=True,
            recognizer=True,
            verbose=True,
        )

        print(
            "[OCR] Reader constructor finished",
            flush=True,
        )

        print(
            "[OCR] EasyOCR Reader ready "
            f"model_dir={model_directory}",
            flush=True,
        )

        return _ocr_reader

# =========================================================
# OCR 이미지 축소 필터
# =========================================================

def get_resize_filter(
    original_size: tuple[int, int],
):
    width, height = original_size

    current_long_side = max(
        width,
        height,
    )

    if current_long_side > (
        OCR_IMAGE_SIDE * 2
    ):
        return Image.Resampling.LANCZOS

    return Image.Resampling.BICUBIC


# =========================================================
# OCR용 이미지 생성
# =========================================================

def create_ocr_image(
    source_image_path: str,
) -> str:
    source_path = Path(
        source_image_path
    )

    ocr_path = (
        source_path.parent
        / (
            f"{source_path.stem}_ocr_"
            f"{uuid.uuid4().hex}.png"
        )
    )

    transposed_image = None
    rgb_image = None
    prepared_image = None
    enhanced_image = None

    try:
        with Image.open(
            source_image_path
        ) as source_image:
            transposed_image = (
                ImageOps.exif_transpose(
                    source_image
                )
            )

            rgb_image = (
                transposed_image.convert(
                    "RGB"
                )
            )

            original_width = (
                rgb_image.width
            )

            original_height = (
                rgb_image.height
            )

            if max(
                rgb_image.size
            ) > OCR_IMAGE_SIDE:
                rgb_image.thumbnail(
                    (
                        OCR_IMAGE_SIDE,
                        OCR_IMAGE_SIDE,
                    ),
                    get_resize_filter(
                        rgb_image.size
                    ),
                    reducing_gap=2.0,
                )

            prepared_image = (
                ImageOps.autocontrast(
                    rgb_image,
                    cutoff=1,
                )
            )

            if OCR_CONTRAST != 1.0:
                contrast_enhancer = (
                    ImageEnhance.Contrast(
                        prepared_image
                    )
                )

                enhanced_image = (
                    contrast_enhancer.enhance(
                        OCR_CONTRAST
                    )
                )

            else:
                enhanced_image = (
                    prepared_image.copy()
                )

            enhanced_image.save(
                ocr_path,
                format="PNG",
                optimize=False,
                compress_level=1,
            )

            print(
                "[OCR] temporary image prepared "
                f"original="
                f"{original_width}x"
                f"{original_height} "
                f"ocr="
                f"{enhanced_image.width}x"
                f"{enhanced_image.height}",
                flush=True,
            )

        return str(
            ocr_path
        )

    finally:
        if enhanced_image is not None:
            try:
                enhanced_image.close()
            except Exception:
                pass

        if prepared_image is not None:
            try:
                prepared_image.close()
            except Exception:
                pass

        if rgb_image is not None:
            try:
                rgb_image.close()
            except Exception:
                pass

        if transposed_image is not None:
            try:
                transposed_image.close()
            except Exception:
                pass


# =========================================================
# OCR 결과 정리
# =========================================================

def normalize_ocr_results(
    results: Any,
) -> list[str]:
    texts: list[str] = []

    if not results:
        return texts

    for item in results:
        text = ""

        if isinstance(
            item,
            str,
        ):
            text = item

        elif (
            isinstance(
                item,
                (list, tuple),
            )
            and len(item) >= 2
        ):
            text = str(
                item[1]
            )

        else:
            text = str(
                item
            )

        text = text.strip()

        if text:
            texts.append(
                text
            )

    return texts


# =========================================================
# 이미지 OCR 실행
# =========================================================

def extract_text_from_image(
    image_path: str,
) -> str:
    ocr_image_path: Optional[str] = None
    results = None

    with _ocr_run_lock:
        try:
            if not image_path:
                print(
                    "[OCR] empty image path",
                    flush=True,
                )
                return ""

            source_path = Path(
                image_path
            )

            if not source_path.exists():
                print(
                    "[OCR] source image not found: "
                    f"{source_path}",
                    flush=True,
                )
                return ""

            if not source_path.is_file():
                print(
                    "[OCR] source path is not a file: "
                    f"{source_path}",
                    flush=True,
                )
                return ""

            print(
                "[OCR] extraction started "
                f"path={source_path}",
                flush=True,
            )

            ocr_image_path = (
                create_ocr_image(
                    str(source_path)
                )
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
                min_size=5,
                rotation_info=None,
                text_threshold=0.5,
                low_text=0.3,
                link_threshold=0.3,
                width_ths=0.7,
                height_ths=0.7,
                contrast_ths=0.05,
                adjust_contrast=0.7,
            )

            texts = (
                normalize_ocr_results(
                    results
                )
            )

            print(
                "[OCR] readtext finished "
                f"count={len(texts)} "
                f"texts={texts}",
                flush=True,
            )

            return "\n".join(
                texts
            )

        except Exception as error:
            print(
                "[OCR] extraction failed: "
                f"{type(error).__name__}: "
                f"{error}",
                flush=True,
            )

            return ""

        finally:
            results = None

            if ocr_image_path:
                try:
                    Path(
                        ocr_image_path
                    ).unlink(
                        missing_ok=True
                    )

                    print(
                        "[OCR] temporary image deleted "
                        f"path={ocr_image_path}",
                        flush=True,
                    )

                except OSError as error:
                    print(
                        "[OCR] temporary image delete failed: "
                        f"{error}",
                        flush=True,
                    )

            gc.collect()


# =========================================================
# OCR Reader 로딩 상태
# =========================================================

def is_ocr_loaded() -> bool:
    return _ocr_reader is not None
