#!/usr/bin/env python3
"""
图片压缩工具
使用PIL/Pillow压缩图片体积，保持清晰度
支持常见图片格式：JPEG, PNG, BMP, TIFF等
支持从MinIO URL下载图片并拼接压缩
重构版本 - 所有逻辑在main函数中
"""


def main(
    process_mode="auto",  # "file", "minio", 或 "auto"
    input_path="",  # 输入路径（可以是单个文件或文件夹，仅在process_mode="file"时使用）
    minio_url_1="http://127.0.0.1:9000/upload/c132f5f7114e6b97565a31fb0830132f.jpg",  # 第一张图片的MinIO地址（将放在上方），如果为空字符串则只处理单张
    minio_url_2="",  # 第二张图片的MinIO地址（将放在下方），如果为空则只处理第一张
    output_path=None,  # 输出路径，如果为None则自动生成
    is_batch=False,  # 是否批量处理（仅在process_mode="file"时有效）
    quality=75,  # JPEG质量 (1-100)，数值越高质量越好但文件越大
    optimize=True,  # 是否优化（更慢但文件更小）
    max_size=None,  # 最大尺寸限制，格式: (宽度, 高度)，如果为None则不调整尺寸
    output_format="JPEG",  # 输出格式: None, "JPEG", "PNG", 或 "AUTO"
    compression_mode="aggressive",  # 压缩模式: "normal", "aggressive", 或 "ultra"
    # MinIO上传配置
    minio_endpoint="127.0.0.1:9000",  # MinIO服务地址（不含http://）
    minio_access_key="minioadmin",  # MinIO访问密钥
    minio_secret_key="minioadmin",  # MinIO秘密密钥
    minio_bucket="upload",  # MinIO存储桶名称
    minio_use_ssl=False,  # 是否使用SSL
    upload_to_minio=True,  # 是否上传到MinIO
    return_url=True,  # 是否返回MinIO URL（如果为False则返回本地路径）
    # 行为开关（与图片拆分_final.py保持一致）
    forbid_redirect=True,        # 下载时禁止 301/302 重定向（防止被强跳 https 导致 SSL 报错）
    auto_infer_from_url=True,    # 从 minio_url_1 自动推断 endpoint/bucket/secure
    preflight_minio_health=True, # 在创建 MinIO 客户端前先探测端口是否 TLS（避免 wrong_version_number）
    timeout=60                   # 请求超时时间
):
    """
    图片压缩工具主函数
    
    参数说明:
    - process_mode: 处理模式
      * "file": 单文件处理（使用input_path）
      * "minio": 从MinIO下载图片并压缩（自动检测单张或两张）
      * "auto": 自动检测模式（如果提供了MinIO地址则使用MinIO模式，否则使用文件模式）
    
    - input_path: 输入路径（可以是单个文件或文件夹，仅在process_mode="file"时使用）
    
    - minio_url_1: 第一张图片的MinIO地址（将放在上方）
      * 如果只提供minio_url_1，则下载并压缩单张图片
      * 如果同时提供minio_url_1和minio_url_2，则下载两张图片并上下拼接后压缩
    
    - minio_url_2: 第二张图片的MinIO地址（将放在下方），如果为空则只处理第一张
    
    - output_path: 输出路径
      * 单文件模式：如果为None则覆盖原文件
      * 批量模式：如果为None则在原文件夹创建compressed子文件夹
      * MinIO单张模式：如果为None则保存为"compressed.jpg"
      * MinIO拼接模式：如果为None则保存为"merged_compressed.jpg"
    
    - is_batch: 是否批量处理（如果input_path是文件夹，会自动识别为批量模式，仅在process_mode="file"时有效）
    
    - quality: JPEG质量 (1-100)，推荐值：75-90（85为平衡点）
    
    - optimize: 是否优化（更慢但文件更小）
    
    - max_size: 最大尺寸限制，格式: (宽度, 高度)，例如: (1920, 1080)，如果为None则不调整尺寸
    
    - output_format: 输出格式
      * None: 自动检测（根据文件扩展名）
      * "JPEG": 强制输出为JPEG格式（推荐，兼容性好）
      * "PNG": 强制输出为PNG格式（支持透明度）
      * "AUTO": 自动尝试JPEG和PNG格式，选择最小的
    
    - compression_mode: 压缩模式
      * "normal": 普通压缩（平衡质量和大小）
      * "aggressive": 激进压缩（更小文件，可能略微降低质量）
      * "ultra": 极致压缩（最小文件，质量可能明显降低）
    """
    # 导入所有必要的库
    import os
    from PIL import Image
    import sys
    from typing import Optional, Tuple
    import requests
    from io import BytesIO
    import tempfile
    import uuid
    from urllib.parse import urlparse
    from datetime import datetime
    
    # 尝试导入minio库
    try:
        from minio import Minio
        from minio.error import S3Error
        MINIO_AVAILABLE = True
    except ImportError:
        MINIO_AVAILABLE = False
        print("⚠️  警告: minio库未安装，无法上传到MinIO。请运行: pip install minio")

    def parse_minio_url(url: str) -> dict:
        """
        从MinIO URL解析配置信息
        
        :param url: MinIO URL，格式: http://endpoint/bucket/object_name
        :return: 包含endpoint, bucket, object_name, use_ssl的字典
        """
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                raise ValueError(f"不是合法URL: {url}")
            
            path_parts = parsed.path.strip('/').split('/', 1)
            bucket = path_parts[0] if path_parts else "upload"
            object_name = path_parts[1] if len(path_parts) > 1 else ""
            endpoint = f"{parsed.hostname}:{parsed.port}" if parsed.port else parsed.hostname
            use_ssl = parsed.scheme.lower() == "https"
            
            return {
                "endpoint": endpoint,
                "bucket": bucket,
                "object_name": object_name,
                "use_ssl": use_ssl
            }
        except Exception as e:
            print(f"⚠️  解析MinIO URL失败: {e}")
            return None

    def detect_env_proxy_hint():
        """
        检测环境变量中的代理配置，给出提示
        服务器若配置代理，可能导致奇怪的 https 行为
        """
        for k in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"]:
            v = os.environ.get(k)
            if v:
                print(f"🧩 环境变量提示: {k}={v}")

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
        """
        标准化超时参数
        requests 允许：None / 数字 / (connect, read)
        """
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

    def upload_to_minio_storage(
        file_path: str,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        use_ssl: bool = False,
        object_name: Optional[str] = None,
        source_filename: Optional[str] = None
    ) -> Optional[str]:
        """
        上传文件到MinIO
        
        :param file_path: 本地文件路径
        :param endpoint: MinIO服务地址
        :param access_key: 访问密钥
        :param secret_key: 秘密密钥
        :param bucket: 存储桶名称
        :param use_ssl: 是否使用SSL
        :param object_name: 对象名称，如果为None则自动生成
        :param source_filename: 源文件名（用于生成唯一名称），如果为None则从file_path提取
        :return: MinIO URL，失败返回None
        """
        if not MINIO_AVAILABLE:
            print("❌ 错误: minio库未安装，无法上传")
            return None
        
        try:
            # 初始化MinIO客户端
            client = Minio(
                endpoint,
                access_key=access_key,
                secret_key=secret_key,
                secure=use_ssl
            )
            
            # 确保bucket存在
            found = client.bucket_exists(bucket)
            if not found:
                print(f"📦 创建存储桶: {bucket}")
                client.make_bucket(bucket)
            
            # 生成对象名称
            if object_name is None:
                # 获取源文件名
                if source_filename:
                    # 从源文件名提取（可能是URL或本地路径）
                    if '/' in source_filename:
                        base_name = os.path.basename(source_filename)
                    else:
                        base_name = source_filename
                else:
                    # 从本地文件路径提取
                    base_name = os.path.basename(file_path)
                
                # 分离文件名和扩展名
                name_without_ext, ext = os.path.splitext(base_name)
                if not ext:
                    ext = ".jpg"  # 默认扩展名
                
                # 清理文件名中的特殊字符（保留字母、数字、下划线、连字符）
                import re
                name_without_ext = re.sub(r'[^a-zA-Z0-9_-]', '_', name_without_ext)
                
                # 生成时间戳（精确到毫秒，格式：YYYYMMDDHHMMSSmmm）
                timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]  # 保留毫秒（3位）
                
                # 源文件名 + 下划线 + 时间戳 + 扩展名
                object_name = f"{name_without_ext}_{timestamp}{ext}"
            
            # 上传文件
            print(f"📤 正在上传到MinIO: {bucket}/{object_name}")
            # 确定content_type
            ext = os.path.splitext(file_path)[1].lower()
            content_type_map = {
                '.jpg': 'image/jpeg',
                '.jpeg': 'image/jpeg',
                '.png': 'image/png',
                '.webp': 'image/webp',
                '.gif': 'image/gif'
            }
            content_type = content_type_map.get(ext, 'image/jpeg')
            
            client.fput_object(
                bucket,
                object_name,
                file_path,
                content_type=content_type
            )
            
            # 生成URL
            protocol = "https" if use_ssl else "http"
            url = f"{protocol}://{endpoint}/{bucket}/{object_name}"
            print(f"✅ 上传成功: {url}")
            
            return url
            
        except S3Error as e:
            print(f"❌ MinIO上传失败: {e}")
            return None
        except Exception as e:
            print(f"❌ 上传失败: {e}")
            return None

    def download_image_from_minio(url: str) -> Optional[Image.Image]:
        """
        从MinIO URL下载图片
        默认禁止重定向（防止 http 被强跳 https 导致 SSL 乱套）。
        
        :param url: MinIO图片地址
        :return: PIL Image对象，失败返回None
        """
        print(f"📥 正在从MinIO下载图片: {url}")
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                stream=True,
                allow_redirects=not forbid_redirect
            )
        except requests.exceptions.SSLError as e:
            print(f"❌ 下载阶段 SSL 错误（很可能把 https 连到了 http 端口，或被代理/网关改写）: {e}")
            return None
        except requests.exceptions.RequestException as e:
            print(f"❌ 下载图片失败: {e}")
            return None

        # 如果禁止重定向，遇到 301/302 直接给出明确错误
        if forbid_redirect and resp.is_redirect:
            loc = resp.headers.get("Location")
            print(f"❌ 下载URL发生重定向({resp.status_code}) -> {loc}")
            print(f"这通常意味着网关把 http 强制跳到 https，导致后续 SSL WRONG_VERSION_NUMBER。")
            print(f"解决：用最终 https URL（若端口支持TLS），或关闭该跳转/换正确端口。")
            return None

        try:
            resp.raise_for_status()
            
            # 从字节流创建图片
            img = Image.open(BytesIO(resp.content))
            print(f"✅ 下载成功: {img.size[0]}x{img.size[1]}")
            return img
        except Exception as e:
            print(f"❌ 处理下载内容失败: {e}")
            return None

    def merge_images_vertically(
        img1: Image.Image,
        img2: Image.Image,
        align: str = "center"
    ) -> Image.Image:
        """
        将两张图片上下拼接
        
        :param img1: 第一张图片（将放在上方）
        :param img2: 第二张图片（将放在下方）
        :param align: 对齐方式 "left", "center", "right"
        :return: 拼接后的图片
        """
        # 统一宽度（以较宽的为准）
        max_width = max(img1.width, img2.width)
        
        # 调整图片宽度，保持宽高比
        def resize_to_width(img, target_width):
            if img.width == target_width:
                return img
            ratio = target_width / img.width
            new_height = int(img.height * ratio)
            return img.resize((target_width, new_height), Image.Resampling.LANCZOS)
        
        img1_resized = resize_to_width(img1, max_width)
        img2_resized = resize_to_width(img2, max_width)
        
        # 创建新图片（高度为两张图片之和）
        total_height = img1_resized.height + img2_resized.height
        merged_img = Image.new('RGB', (max_width, total_height), (255, 255, 255))
        
        # 粘贴第一张图片（上方）
        merged_img.paste(img1_resized, (0, 0))
        
        # 粘贴第二张图片（下方）
        merged_img.paste(img2_resized, (0, img1_resized.height))
        
        print(f"📐 拼接完成: {max_width}x{total_height}")
        return merged_img

    def download_and_compress_from_minio(
        url: str,
        output_path: Optional[str] = None,
        quality: int = 85,
        optimize: bool = True,
        max_size: Optional[Tuple[int, int]] = None,
        format: Optional[str] = None,
        compression_mode: str = "normal"
    ) -> dict:
        """
        从MinIO下载单张图片并压缩
        
        :param url: 图片的MinIO地址
        :param output_path: 输出路径
        :param quality: JPEG质量
        :param optimize: 是否优化
        :param max_size: 最大尺寸
        :param format: 输出格式
        :param compression_mode: 压缩模式
        :return: 处理结果
        """
        try:
            # 下载图片
            print("=" * 60)
            print("📥 开始下载图片...")
            img = download_image_from_minio(url)
            if img is None:
                return {
                    "success": False,
                    "error": f"无法下载图片: {url}"
                }
            
            # 保存临时文件
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
            temp_path = temp_file.name
            temp_file.close()
            
            img.save(temp_path, 'JPEG', quality=95)
            original_size = os.path.getsize(temp_path)
            
            print(f"\n📊 下载后大小: {original_size / 1024:.2f} KB")
            
            # 确定输出路径
            if output_path is None:
                output_path = "compressed.jpg"
            
            # 压缩图片
            print("\n🗜️ 开始压缩图片...")
            result = compress_image(
                temp_path,
                output_path,
                quality=quality,
                optimize=optimize,
                max_size=max_size,
                format=format,
                compression_mode=compression_mode
            )
            
            # 清理临时文件
            if os.path.exists(temp_path):
                os.remove(temp_path)
            
            if result["success"]:
                result["original_size"] = original_size
                result["url"] = url
                result["local_path"] = result.get("output_path")
            
            return result
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def merge_and_compress_from_minio(
        url1: str,
        url2: str,
        output_path: Optional[str] = None,
        quality: int = 85,
        optimize: bool = True,
        max_size: Optional[Tuple[int, int]] = None,
        format: Optional[str] = None,
        compression_mode: str = "normal"
    ) -> dict:
        """
        从MinIO下载两张图片，拼接后压缩
        
        :param url1: 第一张图片的MinIO地址（将放在上方）
        :param url2: 第二张图片的MinIO地址（将放在下方）
        :param output_path: 输出路径
        :param quality: JPEG质量
        :param optimize: 是否优化
        :param max_size: 最大尺寸
        :param format: 输出格式
        :param compression_mode: 压缩模式
        :return: 处理结果
        """
        try:
            # 下载两张图片
            print("=" * 60)
            print("📥 开始下载图片...")
            img1 = download_image_from_minio(url1)
            if img1 is None:
                return {
                    "success": False,
                    "error": f"无法下载第一张图片: {url1}"
                }
            
            img2 = download_image_from_minio(url2)
            if img2 is None:
                return {
                    "success": False,
                    "error": f"无法下载第二张图片: {url2}"
                }
            
            # 拼接图片
            print("\n🔗 开始拼接图片...")
            merged_img = merge_images_vertically(img1, img2)
            
            # 保存临时文件
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
            temp_path = temp_file.name
            temp_file.close()
            
            merged_img.save(temp_path, 'JPEG', quality=95)
            original_size = os.path.getsize(temp_path)
            
            print(f"\n📊 拼接后大小: {original_size / 1024:.2f} KB")
            
            # 确定输出路径
            if output_path is None:
                output_path = "merged_compressed.jpg"
            
            # 压缩拼接后的图片
            print("\n🗜️ 开始压缩拼接后的图片...")
            result = compress_image(
                temp_path,
                output_path,
                quality=quality,
                optimize=optimize,
                max_size=max_size,
                format=format,
                compression_mode=compression_mode
            )
            
            # 清理临时文件
            if os.path.exists(temp_path):
                os.remove(temp_path)
            
            if result["success"]:
                result["original_size"] = original_size
                result["url1"] = url1
                result["url2"] = url2
                result["local_path"] = result.get("output_path")
            
            return result
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def compress_image(
        input_path: str,
        output_path: Optional[str] = None,
        quality: int = 85,
        optimize: bool = True,
        max_size: Optional[Tuple[int, int]] = None,
        format: Optional[str] = None,
        compression_mode: str = "normal"
    ) -> dict:
        """
        压缩图片
        
        :param input_path: 输入图片路径
        :param output_path: 输出图片路径，如果为None则覆盖原文件
        :param quality: JPEG质量 (1-100)，数值越高质量越好但文件越大，默认85
        :param optimize: 是否优化（更慢但文件更小），默认True
        :param max_size: 最大尺寸 (width, height)，如果为None则不调整尺寸
        :param format: 输出格式，如果为None则自动检测
        :return: 压缩结果信息字典
        """
        if not os.path.exists(input_path):
            return {
                "success": False,
                "error": f"文件不存在: {input_path}"
            }
        
        try:
            # 获取原始文件大小
            original_size = os.path.getsize(input_path)
            
            # 根据压缩模式调整参数
            if compression_mode == "aggressive":
                quality = max(60, quality - 10)  # 降低质量
            elif compression_mode == "ultra":
                quality = max(50, quality - 20)  # 大幅降低质量
            
            # 打开图片并预处理
            with Image.open(input_path) as img:
                original_mode = img.mode
                has_transparency = img.mode in ('RGBA', 'LA', 'P')
                
                # 如果需要调整尺寸
                if max_size:
                    img.thumbnail(max_size, Image.Resampling.LANCZOS)
                    print(f"📐 图片尺寸已调整: {img.size}")
                
                # 确定输出路径
                if output_path is None:
                    output_path = input_path
                
                # 确定输出格式
                base_path = os.path.splitext(output_path)[0]
                ext = os.path.splitext(output_path)[1].lower()
                
                # AUTO模式：尝试多种格式，选择最小的
                if format == "AUTO":
                    formats_to_try = ['JPEG']
                    if has_transparency:
                        formats_to_try.insert(0, 'PNG')  # 有透明度时优先尝试PNG
                    else:
                        formats_to_try.append('PNG')  # 无透明度时也尝试PNG
                    
                    best_result = None
                    best_size = float('inf')
                    best_format = None
                    
                    print(f"🔄 AUTO模式：尝试多种格式...")
                    for fmt in formats_to_try:
                        test_path = f"{base_path}_temp.{fmt.lower()}"
                        try:
                            test_img = img.copy()
                            
                            # 准备保存参数
                            save_kwargs = {
                                'format': fmt,
                                'optimize': optimize,
                            }
                            
                            if fmt == 'JPEG':
                                # JPEG需要RGB模式
                                if test_img.mode != 'RGB':
                                    if has_transparency:
                                        background = Image.new('RGB', test_img.size, (255, 255, 255))
                                        if test_img.mode == 'P':
                                            test_img = test_img.convert('RGBA')
                                        background.paste(test_img, mask=test_img.split()[-1] if test_img.mode in ('RGBA', 'LA') else None)
                                        test_img = background
                                    else:
                                        test_img = test_img.convert('RGB')
                                
                                save_kwargs['quality'] = quality
                                save_kwargs['progressive'] = True
                                save_kwargs['exif'] = b''
                            elif fmt == 'PNG':
                                # PNG保持原模式
                                save_kwargs['optimize'] = True
                                save_kwargs['compress_level'] = 9  # 最高压缩级别
                            
                            test_img.save(test_path, **save_kwargs)
                            test_size = os.path.getsize(test_path)
                            
                            print(f"   {fmt}: {test_size / 1024:.2f} KB", end="")
                            
                            if test_size < best_size:
                                if best_result and os.path.exists(best_result):
                                    os.remove(best_result)
                                best_size = test_size
                                best_format = fmt
                                best_result = test_path
                                print(" ✓ (最佳)")
                            else:
                                os.remove(test_path)
                                print()
                            
                        except Exception as e:
                            if os.path.exists(test_path):
                                os.remove(test_path)
                            print(f"   {fmt}: 失败 ({e})")
                    
                    if best_result:
                        # 重命名为最终输出路径
                        final_ext = '.jpg' if best_format == 'JPEG' else '.png'
                        final_path = base_path + final_ext
                        if final_path != best_result:
                            if os.path.exists(final_path):
                                os.remove(final_path)
                            os.rename(best_result, final_path)
                        output_path = final_path
                        format = best_format
                        print(f"✅ 选择最佳格式: {best_format} ({best_size / 1024:.2f} KB)")
                    else:
                        raise Exception("所有格式尝试都失败")
                
                else:
                    # 非AUTO模式：使用指定格式
                    if format is None:
                        format_map = {
                            '.jpg': 'JPEG',
                            '.jpeg': 'JPEG',
                            '.png': 'PNG',
                            '.bmp': 'JPEG',
                            '.tiff': 'JPEG',
                            '.tif': 'JPEG',
                        }
                        format = format_map.get(ext, 'JPEG')
                    
                    # 准备图片模式
                    if format == 'JPEG':
                        # JPEG需要RGB模式
                        if img.mode != 'RGB':
                            if has_transparency:
                                background = Image.new('RGB', img.size, (255, 255, 255))
                                if img.mode == 'P':
                                    img = img.convert('RGBA')
                                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                                img = background
                            else:
                                img = img.convert('RGB')
                    elif format == 'PNG':
                        # PNG保持原模式
                        pass
                    
                    # 保存压缩后的图片
                    save_kwargs = {
                        'format': format,
                        'optimize': optimize,
                    }
                    
                    if format == 'JPEG':
                        save_kwargs['quality'] = quality
                        save_kwargs['progressive'] = True
                        save_kwargs['exif'] = b''
                    elif format == 'PNG':
                        save_kwargs['compress_level'] = 9  # 最高压缩级别
                    
                    # 如果格式改变，更新输出路径扩展名
                    if format == 'JPEG' and not output_path.lower().endswith(('.jpg', '.jpeg')):
                        output_path = os.path.splitext(output_path)[0] + '.jpg'
                    elif format == 'PNG' and not output_path.lower().endswith('.png'):
                        output_path = os.path.splitext(output_path)[0] + '.png'
                    
                    img.save(output_path, **save_kwargs)
            
            # 获取压缩后文件大小
            compressed_size = os.path.getsize(output_path)
            compression_ratio = (1 - compressed_size / original_size) * 100
            
            result = {
                "success": True,
                "original_size": original_size,
                "compressed_size": compressed_size,
                "compression_ratio": compression_ratio,
                "output_path": output_path,
                "format": format
            }
            
            print(f"✅ 压缩完成!")
            print(f"   原始大小: {original_size / 1024:.2f} KB")
            print(f"   压缩后: {compressed_size / 1024:.2f} KB")
            print(f"   压缩率: {compression_ratio:.1f}%")
            print(f"   输出格式: {format}")
            print(f"   输出路径: {output_path}")
            
            return result
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }

    def batch_compress(
        input_folder: str,
        output_folder: Optional[str] = None,
        quality: int = 85,
        optimize: bool = True,
        max_size: Optional[Tuple[int, int]] = None,
        format: Optional[str] = None,
        compression_mode: str = "normal"
    ) -> dict:
        """
        批量压缩图片
        
        :param input_folder: 输入文件夹路径
        :param output_folder: 输出文件夹路径，如果为None则在原文件夹创建compressed子文件夹
        :param quality: JPEG质量
        :param optimize: 是否优化
        :param max_size: 最大尺寸
        :param format: 输出格式
        :return: 批量处理结果
        """
        if not os.path.isdir(input_folder):
            return {
                "success": False,
                "error": f"文件夹不存在: {input_folder}"
            }
        
        # 支持的图片格式
        image_extensions = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif', '.webp'}
        
        # 获取所有图片文件
        image_files = [
            f for f in os.listdir(input_folder)
            if any(f.lower().endswith(ext) for ext in image_extensions)
        ]
        
        if not image_files:
            return {
                "success": False,
                "error": "文件夹中没有找到支持的图片文件"
            }
        
        # 确定输出文件夹
        if output_folder is None:
            output_folder = os.path.join(input_folder, "compressed")
        os.makedirs(output_folder, exist_ok=True)
        
        print(f"📁 找到 {len(image_files)} 个图片文件")
        print(f"📂 输出文件夹: {output_folder}\n")
        
        results = {
            "total": len(image_files),
            "success": 0,
            "failed": 0,
            "details": []
        }
        
        for i, filename in enumerate(image_files, 1):
            input_path_item = os.path.join(input_folder, filename)
            output_path_item = os.path.join(output_folder, filename)
            
            print(f"[{i}/{len(image_files)}] 处理: {filename}")
            result = compress_image(
                input_path_item,
                output_path_item,
                quality=quality,
                optimize=optimize,
                max_size=max_size,
                format=format,
                compression_mode=compression_mode
            )
            
            if result["success"]:
                results["success"] += 1
            else:
                results["failed"] += 1
                print(f"❌ 失败: {result.get('error', '未知错误')}")
            
            results["details"].append({
                "filename": filename,
                "result": result
            })
            print()
        
        print("=" * 60)
        print(f"📊 批量处理完成:")
        print(f"   总计: {results['total']} 个文件")
        print(f"   成功: {results['success']} 个")
        print(f"   失败: {results['failed']} 个")
        print("=" * 60)
        
        return results

    # 自动检测模式
    if process_mode == "auto":
        # 如果提供了MinIO地址，自动切换到MinIO模式
        if minio_url_1:
            process_mode = "minio"
        else:
            process_mode = "file"
    
    # 显示配置信息
    print("=" * 60)
    print("📋 压缩配置:")
    if process_mode == "minio":
        # 检测是单张还是两张图片
        has_url_1 = minio_url_1 and minio_url_1.strip()
        has_url_2 = minio_url_2 and minio_url_2.strip()
        
        if has_url_1 and has_url_2:
            print(f"   处理模式: MinIO图片拼接压缩（两张图片）")
            print(f"   MinIO地址1: {minio_url_1}")
            print(f"   MinIO地址2: {minio_url_2}")
        elif has_url_1:
            print(f"   处理模式: MinIO图片下载压缩（单张图片）")
            print(f"   MinIO地址: {minio_url_1}")
        else:
            print(f"   处理模式: MinIO模式（但未提供地址）")
    else:
        print(f"   输入路径: {input_path}")
        print(f"   处理模式: {'批量处理' if is_batch or (input_path and os.path.isdir(input_path)) else '单文件处理'}")
    print(f"   输出路径: {output_path if output_path else '自动'}")
    print(f"   压缩模式: {compression_mode}")
    print(f"   JPEG质量: {quality}")
    print(f"   优化: {'是' if optimize else '否'}")
    print(f"   最大尺寸: {max_size if max_size else '无限制'}")
    print(f"   输出格式: {output_format if output_format else '自动检测'}")
    print("=" * 60)
    print()
    
    # 根据处理模式执行相应操作
    if process_mode == "minio":
        # MinIO模式：自动检测单张或两张图片
        has_url_1 = minio_url_1 and minio_url_1.strip()
        has_url_2 = minio_url_2 and minio_url_2.strip()
        
        if not has_url_1:
            print("❌ 错误: MinIO模式需要至少提供minio_url_1")
            sys.exit(1)
        
        if has_url_1 and has_url_2:
            # 两张图片：拼接压缩
            result = merge_and_compress_from_minio(
                minio_url_1,
                minio_url_2,
                output_path,
                quality=quality,
                optimize=optimize,
                max_size=max_size,
                format=output_format,
                compression_mode=compression_mode
            )
        else:
            # 单张图片：直接下载压缩
            result = download_and_compress_from_minio(
                minio_url_1,
                output_path,
                quality=quality,
                optimize=optimize,
                max_size=max_size,
                format=output_format,
                compression_mode=compression_mode
            )
        
        if not result["success"]:
            print(f"❌ 处理失败: {result.get('error', '未知错误')}")
            sys.exit(1)
        
        # 上传到MinIO
        final_url = None
        if upload_to_minio and result.get("local_path"):
            # 尝试从输入URL解析MinIO配置
            parsed_config = None
            source_filename = None
            if minio_url_1:
                parsed_config = parse_minio_url(minio_url_1)
                # 从URL提取源文件名
                if parsed_config and parsed_config.get("object_name"):
                    source_filename = parsed_config["object_name"]
                else:
                    # 从URL路径提取文件名
                    source_filename = os.path.basename(urlparse(minio_url_1).path)
            
            # 使用解析的配置或提供的配置
            upload_endpoint = parsed_config["endpoint"] if parsed_config else minio_endpoint
            upload_bucket = parsed_config["bucket"] if parsed_config else minio_bucket
            upload_use_ssl = parsed_config["use_ssl"] if parsed_config else minio_use_ssl
            
            print("\n" + "=" * 60)
            print("📤 开始上传到MinIO...")
            final_url = upload_to_minio_storage(
                result["local_path"],
                upload_endpoint,
                minio_access_key,
                minio_secret_key,
                upload_bucket,
                upload_use_ssl,
                source_filename=source_filename
            )
            
            if final_url:
                result["minio_url"] = final_url
                # 如果return_url为True，清理本地文件
                if return_url and os.path.exists(result["local_path"]):
                    try:
                        os.remove(result["local_path"])
                        print(f"🗑️  已删除本地文件: {result['local_path']}")
                    except Exception as e:
                        print(f"⚠️  删除本地文件失败: {e}")
        
        # 返回结果
        if return_url and final_url:
            print("\n" + "=" * 60)
            print(f"✅ 处理完成，MinIO URL: {final_url}")
            print("=" * 60)
            return final_url
        elif result.get("local_path"):
            print("\n" + "=" * 60)
            print(f"✅ 处理完成，本地路径: {result['local_path']}")
            print("=" * 60)
            return result["local_path"]
        else:
            return result
    else:
        # 文件处理模式
        if not input_path:
            print("❌ 错误: 文件处理模式需要提供输入路径")
            sys.exit(1)
        
        # 判断是批量处理还是单文件处理
        if is_batch or os.path.isdir(input_path):
            # 批量处理
            batch_compress(
                input_path,
                output_path,
                quality=quality,
                optimize=optimize,
                max_size=max_size,
                format=output_format,
                compression_mode=compression_mode
            )
        else:
            # 单文件处理
            result = compress_image(
                input_path,
                output_path,
                quality=quality,
                optimize=optimize,
                max_size=max_size,
                format=output_format,
                compression_mode=compression_mode
            )
            
            if not result["success"]:
                print(f"❌ 压缩失败: {result.get('error', '未知错误')}")
                sys.exit(1)
            
            # 上传到MinIO
            final_url = None
            if upload_to_minio and result.get("output_path"):
                # 从输入路径提取源文件名
                source_filename = os.path.basename(input_path) if input_path else None
                
                print("\n" + "=" * 60)
                print("📤 开始上传到MinIO...")
                final_url = upload_to_minio_storage(
                    result["output_path"],
                    minio_endpoint,
                    minio_access_key,
                    minio_secret_key,
                    minio_bucket,
                    minio_use_ssl,
                    source_filename=source_filename
                )
                
                if final_url:
                    result["minio_url"] = final_url
                    # 如果return_url为True，清理本地文件
                    if return_url and os.path.exists(result["output_path"]):
                        try:
                            os.remove(result["output_path"])
                            print(f"🗑️  已删除本地文件: {result['output_path']}")
                        except Exception as e:
                            print(f"⚠️  删除本地文件失败: {e}")
            
            # 返回结果
            if return_url and final_url:
                print("\n" + "=" * 60)
                print(f"✅ 处理完成，MinIO URL: {final_url}")
                print("=" * 60)
                return final_url
            elif result.get("output_path"):
                print("\n" + "=" * 60)
                print(f"✅ 处理完成，本地路径: {result['output_path']}")
                print("=" * 60)
                return result["output_path"]
            else:
                return result
