#!/usr/bin/env python3
"""
图片拆解器（增强稳定版）
- 根据坐标截取图片并上传到 MinIO
- 支持图片压缩功能（可配置质量、尺寸限制等）
- 重点修复：服务器环境常见的 SSL WRONG_VERSION_NUMBER（http/https 协议错配）
- 所有逻辑仍在 main 内（按你原风格），但更健壮
"""

def main(
    input_image_url="http://127.0.0.1:9000/upload/c132f5f7114e6b97565a31fb0830132f.jpg",
    crop_coordinates=[(100, 100, 400, 400), (500, 200, 800, 600), (100, 500, 500, 900)],

    # MinIO 配置（仍可传；但如果 input_image_url 是 MinIO URL，将自动覆盖推断）
    minio_endpoint="127.0.0.1:9000",
    minio_access_key="minioadmin",
    minio_secret_key="minioadmin",
    minio_bucket="upload",
    minio_secure=False,

    timeout=60,
    output_prefix="cropped",

    # 行为开关
    forbid_redirect=True,        # 下载时禁止 301/302 重定向（防止被强跳 https 导致 SSL 报错）
    auto_infer_from_url=True,    # 从 input_image_url 自动推断 endpoint/bucket/secure
    preflight_minio_health=True, # 在创建 MinIO 客户端前先探测端口是否 TLS（避免 wrong_version_number）
    cleanup_downloaded=True,     # 处理后删除下载的临时文件

    # 图片压缩配置
    enable_compression=True,      # 是否启用压缩（默认启用，生产环境推荐）
    compression_quality=85,       # JPEG质量 (1-100)，默认85（平衡质量和大小）
    compression_optimize=True,    # 是否优化（更慢但文件更小），默认True
    compression_max_size=None,   # 最大尺寸限制，格式: (宽度, 高度)，None表示不限制
    compression_mode="normal"    # 压缩模式: "normal"(普通), "aggressive"(激进), "ultra"(极致)
):
    import os
    import io
    import uuid
    import time
    import requests
    from urllib.parse import urlparse
    from typing import Optional, List, Tuple, Dict, Any
    from PIL import Image

    from minio import Minio
    from minio.error import S3Error

    # ==================== 工具函数 ====================

    def is_url(s: str) -> bool:
        return s.startswith("http://") or s.startswith("https://")

    def normalize_coordinates(coordinates: Any):
        """
        统一把各种坐标输入转成: List[Tuple[int,int,int,int]]
        支持：
        - (x1,y1,x2,y2)
        - [(x1,y1,x2,y2), ...]
        - [x1,y1,x2,y2]
        - [[x1,y1,x2,y2], ...]
        - [[x1,y1],[x2,y2]]  # 两点形式
        - {"x1":..,"y1":..,"x2":..,"y2":..}
        - {"left":..,"top":..,"right":..,"bottom":..}
        - {"x":..,"y":..,"w":..,"h":..}  # 宽高形式
        - 以上任意形式的 JSON 字符串
        """
        import json

        def to_int(v):
            if isinstance(v, bool):
                raise ValueError("坐标不能是bool")
            if isinstance(v, (int, float)):
                return int(v)
            if isinstance(v, str):
                s = v.strip()
                # 允许 "100" / "100.0"
                return int(float(s))
            raise ValueError(f"坐标值无法转成数字: {v} ({type(v)})")

        def one_box(x1, y1, x2, y2):
            x1, y1, x2, y2 = map(to_int, (x1, y1, x2, y2))
            return (x1, y1, x2, y2)

        # 1) 如果是字符串，先尝试当 JSON 解析；不行再用 Python 字面量解析
        if isinstance(coordinates, str):
            import json
            import ast

            s = coordinates.strip()

            # 1.1 先尝试 JSON
            try:
                coordinates = json.loads(s)
            except Exception:
                # 1.2 再尝试 Python 字面量（支持 [(100,100,400,400), ...] / (100,100,400,400) 等）
                try:
                    coordinates = ast.literal_eval(s)
                except Exception:
                    # 1.3 也支持 "100,100,400,400" 这种逗号串
                    if "," in s:
                        parts = [p.strip() for p in s.split(",") if p.strip()]
                        if len(parts) == 4:
                            return [one_box(*parts)]
                    raise ValueError(f"坐标字符串无法解析: {coordinates}")

        # 2) dict：支持多种 key
        if isinstance(coordinates, dict):
            # 可能是单个框，也可能是 {"boxes":[...]}
            if "boxes" in coordinates and isinstance(coordinates["boxes"], list):
                return normalize_coordinates(coordinates["boxes"])

            keys1 = ("x1", "y1", "x2", "y2")
            keys2 = ("left", "top", "right", "bottom")
            keys3 = ("x", "y", "w", "h")

            if all(k in coordinates for k in keys1):
                return [one_box(coordinates["x1"], coordinates["y1"], coordinates["x2"], coordinates["y2"])]

            if all(k in coordinates for k in keys2):
                return [one_box(coordinates["left"], coordinates["top"], coordinates["right"], coordinates["bottom"])]

            if all(k in coordinates for k in keys3):
                x = coordinates["x"];
                y = coordinates["y"];
                w = coordinates["w"];
                h = coordinates["h"]
                return [one_box(x, y, to_int(x) + to_int(w), to_int(y) + to_int(h))]

            raise ValueError(f"坐标 dict 不支持的结构: {coordinates}")

        # 3) tuple：单框
        if isinstance(coordinates, tuple):
            if len(coordinates) == 4:
                return [one_box(*coordinates)]
            raise ValueError(f"坐标 tuple 长度必须为4: {coordinates}")

        # 4) list：可能是单框 / 多框 / 两点形式
        if isinstance(coordinates, list):
            if len(coordinates) == 0:
                raise ValueError("坐标列表为空")

            # 4.1 单框: [x1,y1,x2,y2]
            if len(coordinates) == 4 and all(not isinstance(x, (list, dict, tuple)) for x in coordinates):
                return [one_box(*coordinates)]

            # 4.2 两点形式: [[x1,y1],[x2,y2]]
            if len(coordinates) == 2 and all(isinstance(x, (list, tuple)) and len(x) == 2 for x in coordinates):
                (x1, y1), (x2, y2) = coordinates
                return [one_box(x1, y1, x2, y2)]

            # 4.3 多框: [[x1,y1,x2,y2], ...] 或 [(...), ...] 或 [{"x1":...}, ...]
            boxes = []
            for item in coordinates:
                boxes.extend(normalize_coordinates(item))
            return boxes

        raise ValueError(f"坐标类型不支持: {type(coordinates)} value={coordinates}")

    def parse_minio_url(url: str) -> Tuple[str, str, str, bool]:
        """
        解析形如：http(s)://endpoint/bucket/object 的 URL
        返回：endpoint, bucket, object_name, use_ssl
        """
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"不是合法URL: {url}")

        endpoint = parsed.netloc  # host:port
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if len(path_parts) < 1 or not path_parts[0]:
            raise ValueError(f"URL缺少bucket: {url}")
        bucket = path_parts[0]
        object_name = path_parts[1] if len(path_parts) > 1 else ""
        use_ssl = parsed.scheme.lower() == "https"
        return endpoint, bucket, object_name, use_ssl

    def detect_env_proxy_hint():
        # 不强依赖，但给你提示：服务器若配置代理，可能导致奇怪的 https 行为
        for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"]:
            v = os.environ.get(k)
            if v:
                print(f"🧩 环境变量提示: {k}={v}")

    def download_image_from_url(url: str, local_path: Optional[str] = None) -> str:
        """
        下载图片到本地。默认禁止重定向（防止 http 被强跳 https 导致 SSL 乱套）。
        """
        print(f"📥 开始下载图片: {url}")
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                stream=True,
                allow_redirects=not forbid_redirect
            )
        except requests.exceptions.SSLError as e:
            raise Exception(
                f"下载阶段 SSL 错误（很可能把 https 连到了 http 端口，或被代理/网关改写）: {e}"
            )
        except requests.exceptions.RequestException as e:
            raise Exception(f"下载图片失败: {e}")

        # 如果禁止重定向，遇到 301/302 直接给出明确错误
        if forbid_redirect and resp.is_redirect:
            loc = resp.headers.get("Location")
            raise Exception(
                f"下载URL发生重定向({resp.status_code}) -> {loc}\n"
                f"这通常意味着网关把 http 强制跳到 https，导致后续 SSL WRONG_VERSION_NUMBER。\n"
                f"解决：用最终 https URL（若端口支持TLS），或关闭该跳转/换正确端口。"
            )

        resp.raise_for_status()

        if local_path is None:
            filename = os.path.basename(urlparse(url).path)
            if not filename or "." not in filename:
                filename = f"downloaded_{uuid.uuid4().hex[:8]}.jpg"
            local_path = filename

        os.makedirs(os.path.dirname(local_path) if os.path.dirname(local_path) else ".", exist_ok=True)

        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        size_kb = os.path.getsize(local_path) / 1024
        print(f"✅ 图片下载完成: {local_path} (大小: {size_kb:.2f} KB)")
        return local_path

    def http_health_probe(endpoint: str, use_ssl: bool) -> bool:
        """
        探测 MinIO 健康接口是否可用，用于判断端口是否支持 TLS。
        - 返回 True 表示探测成功
        - 返回 False 表示失败（由调用方决定如何处理）
        """
        scheme = "https" if use_ssl else "http"
        url = f"{scheme}://{endpoint}/minio/health/live"
        try:
            r = requests.get(url, timeout=10, allow_redirects=False)
            # 200 为健康；403/401 有时也可能出现（网关拦截），但至少说明协议层通了
            if r.status_code in (200, 204, 401, 403):
                print(f"🩺 MinIO 健康探测成功: {url} (status={r.status_code})")
                return True
            print(f"🩺 MinIO 健康探测返回异常状态: {url} (status={r.status_code})")
            return False
        except requests.exceptions.SSLError as e:
            print(f"🩺 MinIO 健康探测 SSL 失败: {url} -> {e}")
            return False
        except Exception as e:
            print(f"🩺 MinIO 健康探测失败: {url} -> {e}")
            return False

    def auto_fix_secure_by_probe(endpoint: str, secure_guess: bool) -> bool:
        """
        如果猜测的 secure 不对，尝试用 health probe 反推。
        逻辑：
        - 先用 secure_guess 探测
        - 若失败，再用相反协议探测
        - 若相反成功，则切换 secure
        - 若都失败，保留 secure_guess（让后续 MinIO SDK 报更具体错误）
        """
        if not preflight_minio_health:
            return secure_guess

        ok = http_health_probe(endpoint, secure_guess)
        if ok:
            return secure_guess

        flipped = not secure_guess
        ok2 = http_health_probe(endpoint, flipped)
        if ok2:
            print(f"🔁 自动修正协议: secure {secure_guess} -> {flipped}（避免 WRONG_VERSION_NUMBER）")
            return flipped

        print("⚠️ 无法通过健康探测确认 MinIO 协议（可能被网关拦截/非标准路径/网络不通），将继续使用当前 secure 设置。")
        return secure_guess

    def _normalize_timeout(t):
        # requests 允许：None / 数字 / (connect, read)
        if t is None:
            return None
        if isinstance(t, (int, float)):
            return float(t)
        if isinstance(t, str):
            return float(t.strip())
        if isinstance(t, (tuple, list)) and len(t) == 2:
            c, r = t
            c = float(c.strip()) if isinstance(c, str) else float(c)
            r = float(r.strip()) if isinstance(r, str) else float(r)
            return (c, r)
        raise ValueError(f"timeout 参数类型不支持: {type(t)} value={t}")

    # ==================== 核心类 ====================

    class ImageSplitter:
        def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, secure: bool,
                     enable_compression: bool = True, compression_quality: int = 85,
                     compression_optimize: bool = True, compression_max_size: Optional[Tuple[int, int]] = None,
                     compression_mode: str = "normal"):
            self.minio_endpoint = endpoint
            self.minio_bucket = bucket
            self.minio_secure = secure

            # 压缩配置
            self.enable_compression = enable_compression
            self.compression_quality = compression_quality
            self.compression_optimize = compression_optimize
            self.compression_max_size = compression_max_size
            self.compression_mode = compression_mode

            # 先做协议探测（必要时自动翻转 secure）
            self.minio_secure = auto_fix_secure_by_probe(self.minio_endpoint, self.minio_secure)

            try:
                self.minio_client = Minio(
                    self.minio_endpoint,
                    access_key=access_key,
                    secret_key=secret_key,
                    secure=self.minio_secure
                )
            except Exception as e:
                raise Exception(f"MinIO客户端初始化失败: {e}")

            print("🚀 MinIO客户端初始化完成")
            print(f"📡 MinIO地址: {self.minio_endpoint}")
            print(f"🔐 使用HTTPS: {self.minio_secure}")
            print(f"🗂️ 存储桶: {self.minio_bucket}")
            if self.enable_compression:
                print(f"🗜️ 压缩功能: 已启用 (质量={self.compression_quality}, 模式={self.compression_mode})")
                if self.compression_max_size:
                    print(f"📐 最大尺寸: {self.compression_max_size[0]}x{self.compression_max_size[1]}")

            self._test_connection()

        def _test_connection(self):
            try:
                if self.minio_client.bucket_exists(self.minio_bucket):
                    print(f"✅ MinIO连接成功，存储桶 '{self.minio_bucket}' 存在")
                else:
                    print(f"⚠️ 存储桶 '{self.minio_bucket}' 不存在，将尝试创建")
                    self.minio_client.make_bucket(self.minio_bucket)
                    print(f"✅ 存储桶 '{self.minio_bucket}' 创建成功")
            except S3Error as e:
                # 如果是 SSL WRONG_VERSION_NUMBER，这里也会更明确
                raise Exception(f"MinIO连接/桶检测失败: {e}")
            except Exception as e:
                raise Exception(f"MinIO连接失败: {e}")

        def crop_image(self, image_path: str, coordinates: Tuple[int, int, int, int]) -> Image.Image:
            x1, y1, x2, y2 = coordinates

            if x1 >= x2 or y1 >= y2:
                raise ValueError(f"坐标无效: {coordinates}，右下角必须大于左上角")

            print(f"✂️ 截取区域: ({x1}, {y1}) -> ({x2}, {y2})")

            with Image.open(image_path) as img:
                img_width, img_height = img.size
                print(f"📐 原图尺寸: {img_width} x {img_height}")

                if x2 > img_width or y2 > img_height:
                    print("⚠️ 截取坐标超出图片范围，将自动调整")
                    x2 = min(x2, img_width)
                    y2 = min(y2, img_height)
                    print(f"📐 调整后坐标: ({x1}, {y1}) -> ({x2}, {y2})")

                cropped = img.crop((x1, y1, x2, y2))
                print(f"✅ 截取完成: {cropped.size[0]} x {cropped.size[1]}")
                return cropped

        def compress_image(self, image: Image.Image) -> Image.Image:
            """
            压缩图片
            - 根据压缩模式调整质量参数
            - 支持尺寸限制
            - 保持图片质量的同时减小文件大小
            """
            if not self.enable_compression:
                return image

            print(f"🗜️ 开始压缩图片...")
            original_size = image.size

            # 显示压缩模式信息（实际质量调整在upload_to_minio中应用）
            if self.compression_mode == "aggressive":
                print(f"   压缩模式: 激进 (质量将调整为 {max(60, self.compression_quality - 10)})")
            elif self.compression_mode == "ultra":
                print(f"   压缩模式: 极致 (质量将调整为 {max(50, self.compression_quality - 20)})")
            else:
                print(f"   压缩模式: 普通 (质量 {self.compression_quality})")

            # 处理图片模式（确保兼容性）
            if image.mode in ("RGBA", "LA"):
                bg = Image.new("RGB", image.size, (255, 255, 255))
                bg.paste(image, mask=image.split()[-1])
                processed_img = bg
            elif image.mode == "P":
                tmp = image.convert("RGBA")
                bg = Image.new("RGB", tmp.size, (255, 255, 255))
                bg.paste(tmp, mask=tmp.split()[-1])
                processed_img = bg
            else:
                processed_img = image.convert("RGB") if image.mode != "RGB" else image

            # 应用尺寸限制
            if self.compression_max_size:
                max_width, max_height = self.compression_max_size
                img_width, img_height = processed_img.size
                
                if img_width > max_width or img_height > max_height:
                    # 计算缩放比例，保持宽高比
                    ratio = min(max_width / img_width, max_height / img_height)
                    new_width = int(img_width * ratio)
                    new_height = int(img_height * ratio)
                    
                    print(f"   尺寸调整: {img_width}x{img_height} -> {new_width}x{new_height}")
                    processed_img = processed_img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            print(f"✅ 压缩预处理完成: {processed_img.size[0]}x{processed_img.size[1]}")
            return processed_img

        def upload_to_minio(self, image: Image.Image, object_name: Optional[str] = None) -> str:
            if object_name is None:
                object_name = f"{uuid.uuid4().hex}.jpg"

            print(f"📤 上传图片到MinIO: {object_name}")

            # 应用压缩（如果启用）- compress_image已经处理了模式转换和尺寸调整
            if self.enable_compression:
                image = self.compress_image(image)
                # compress_image已返回RGB模式，直接使用
                image_to_save = image
            else:
                # 未启用压缩时，需要手动处理模式转换
                # ✅ 关键修复：JPEG 不支持 RGBA/LA 等带透明通道的模式，统一转成 RGB
                if image.mode in ("RGBA", "LA"):
                    # 透明图：用白底合成
                    bg = Image.new("RGB", image.size, (255, 255, 255))
                    bg.paste(image, mask=image.split()[-1])  # alpha 通道做 mask
                    image_to_save = bg
                elif image.mode == "P":
                    # 调色板模式可能带透明，先转 RGBA 再合成
                    tmp = image.convert("RGBA")
                    bg = Image.new("RGB", tmp.size, (255, 255, 255))
                    bg.paste(tmp, mask=tmp.split()[-1])
                    image_to_save = bg
                else:
                    # 其他模式直接转 RGB 更稳（例如 CMYK、L）
                    image_to_save = image.convert("RGB") if image.mode != "RGB" else image

            # 根据压缩设置确定质量参数
            quality = self.compression_quality if self.enable_compression else 95
            if self.enable_compression:
                if self.compression_mode == "aggressive":
                    quality = max(60, self.compression_quality - 10)
                elif self.compression_mode == "ultra":
                    quality = max(50, self.compression_quality - 20)

            buf = io.BytesIO()
            save_kwargs = {
                "format": "JPEG",
                "quality": quality,
                "optimize": self.compression_optimize if self.enable_compression else False
            }
            image_to_save.save(buf, **save_kwargs)
            buf.seek(0)
            file_size = buf.getbuffer().nbytes

            try:
                self.minio_client.put_object(
                    bucket_name=self.minio_bucket,
                    object_name=object_name,
                    data=buf,
                    length=file_size,
                    content_type="image/jpeg"
                )
            except S3Error as e:
                raise Exception(f"MinIO上传失败: {e}")
            except Exception as e:
                raise Exception(f"上传图片失败: {e}")

            protocol = "https" if self.minio_secure else "http"
            url = f"{protocol}://{self.minio_endpoint}/{self.minio_bucket}/{object_name}"
            print(f"✅ 上传成功: {url} (大小: {file_size / 1024:.2f} KB)")
            return url

        def process_image(self, image_url: str, coordinates: Any, output_prefix: str) -> Dict[str, Any]:
            downloaded_file = None
            try:
                print("\n" + "=" * 60)
                print("🎯 开始处理图片")
                print("=" * 60)

                downloaded_file = download_image_from_url(image_url)

                coordinates_list = normalize_coordinates(coordinates)

                print("\n📊 处理任务:")
                print(f"   输入图片: {image_url}")
                print(f"   截取区域数: {len(coordinates_list)}")

                results = []
                for i, coords in enumerate(coordinates_list, 1):
                    print(f"\n--- 处理第 {i}/{len(coordinates_list)} 个区域 ---")
                    cropped = self.crop_image(downloaded_file, coords)

                    original_name = os.path.splitext(os.path.basename(downloaded_file))[0]
                    output_name = f"{output_prefix}_{original_name}_{i}_{uuid.uuid4().hex[:8]}.jpg"
                    url = self.upload_to_minio(cropped, output_name)

                    # 记录压缩信息
                    compression_info = {}
                    if enable_compression:
                        compression_info = {
                            "compression_enabled": True,
                            "compression_quality": compression_quality,
                            "compression_mode": compression_mode
                        }
                        if compression_max_size:
                            compression_info["max_size"] = compression_max_size

                    results.append({
                        "index": i,
                        "coordinates": coords,
                        "minio_url": url,
                        "size": cropped.size,
                        **compression_info
                    })

                print("\n" + "=" * 60)
                print("✅ 处理完成!")
                print("=" * 60)

                return {
                    "success": True,
                    "input_url": image_url,
                    "total_crops": len(results),
                    "results": results,
                    "minio_endpoint": self.minio_endpoint,
                    "minio_secure": self.minio_secure,
                    "minio_bucket": self.minio_bucket,
                    "compression_enabled": self.enable_compression,
                    "compression_quality": self.compression_quality,
                    "compression_mode": self.compression_mode
                }

            except Exception as e:
                return {"success": False, "error": str(e)}
            finally:
                if cleanup_downloaded and downloaded_file and os.path.exists(downloaded_file):
                    try:
                        os.remove(downloaded_file)
                        print(f"🗑️ 已删除临时文件: {downloaded_file}")
                    except Exception as e:
                        print(f"⚠️ 删除临时文件失败: {e}")

    # ==================== 主逻辑 ====================

    timeout = _normalize_timeout(timeout)

    detect_env_proxy_hint()

    # 1) 自动从 input_image_url 推断 MinIO 配置（优先级最高）
    if auto_infer_from_url and is_url(input_image_url):
        try:
            ep, bucket, obj, use_ssl = parse_minio_url(input_image_url)
            # 如果 URL 看起来是 MinIO 对象 URL，就用它来对齐 endpoint/bucket/secure
            if bucket:
                print("🧠 已从 input_image_url 自动推断 MinIO 配置（避免协议错配）")
                minio_endpoint = ep or minio_endpoint
                minio_bucket = bucket or minio_bucket
                minio_secure = use_ssl
        except Exception as e:
            # 不是标准 MinIO URL 也没关系：继续用传入的配置
            print(f"ℹ️ 未从URL推断 MinIO 配置（继续使用传入参数）: {e}")

    # 2) 创建 splitter（内部会做 health probe，必要时自动翻转 secure）
    try:
        splitter = ImageSplitter(
            endpoint=minio_endpoint,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            bucket=minio_bucket,
            secure=minio_secure,
            enable_compression=enable_compression,
            compression_quality=compression_quality,
            compression_optimize=compression_optimize,
            compression_max_size=compression_max_size,
            compression_mode=compression_mode
        )
    except Exception as e:
        # 这里把最常见错误给你更明确的提示
        msg = str(e)
        if "WRONG_VERSION_NUMBER" in msg or "wrong version number" in msg:
            raise Exception(
                f"{msg}\n\n"
                f"✅ 这几乎一定是 http/https 协议与端口不匹配：\n"
                f"- 你在用 HTTPS 去连一个 HTTP 端口（常见 9000）\n"
                f"- 或者网关把 http 强制跳 https，但该端口并不支持 TLS\n"
                f"建议：确认 MinIO 对外的真实 URL（http 还是 https、端口是多少），并让 input_image_url 与上传 endpoint 协议一致。"
            )
        raise

    # 3) 处理图片
    result = splitter.process_image(
        image_url=input_image_url,
        coordinates=crop_coordinates,
        output_prefix=output_prefix
    )

    # 4) 打印摘要
    if result.get("success"):
        print("\n📋 处理结果摘要:")
        print(f"   输入图片: {result['input_url']}")
        print(f"   截取数量: {result['total_crops']}")
        print(f"   MinIO地址: {result['minio_endpoint']}  HTTPS={result['minio_secure']}  bucket={result['minio_bucket']}")
        if result.get("compression_enabled"):
            print(f"   压缩设置: 已启用 (质量={result.get('compression_quality')}, 模式={result.get('compression_mode')})")
        print("\n📎 输出MinIO链接:")
        for item in result["results"]:
            print(f"   [{item['index']}] {item['minio_url']}")
            print(f"       坐标: {item['coordinates']}, 尺寸: {item['size'][0]}x{item['size'][1]}")
    else:
        print(f"\n❌ 处理失败: {result.get('error')}")

    return result
