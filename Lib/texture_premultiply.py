"""
纹理预乘模块（用于 Live2D OpenGL 渲染修复）
将纹理像素的 RGB 乘以 Alpha，消除图集边缘采样污染。
"""

import os
import numpy as np
from PIL import Image
import OpenGL.GL as gl

# 各向异性扩展常量
GL_TEXTURE_MAX_ANISOTROPY_EXT = 0x84FE
GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT = 0x84FF

# 记录已成功预乘并上传的纹理绝对路径（不保存像素数据）
_processed = set()


def _premultiply_image_data(image_path):
    try:
        img = Image.open(image_path).convert('RGBA')
    except Exception as e:
        raise RuntimeError(f"无法加载图片 {image_path}: {e}")

    data = np.array(img, dtype=np.uint8)
    # 检查 alpha 是否全为 255（完全不透明）
    if np.all(data[:, :, 3] == 255):
        # 无需预乘，直接返回原始数据
        return data.shape[1], data.shape[0], data

    # 原位预乘：RGB 分别乘以 Alpha / 255
    # 使用 float 进行精度计算，避免整数溢出
    alpha_float = data[:, :, 3].astype(np.float32) / 255.0
    data[:, :, 0] = (data[:, :, 0].astype(np.float32) * alpha_float).astype(np.uint8)
    data[:, :, 1] = (data[:, :, 1].astype(np.float32) * alpha_float).astype(np.uint8)
    data[:, :, 2] = (data[:, :, 2].astype(np.float32) * alpha_float).astype(np.uint8)

    # 显式释放临时变量
    del img, alpha_float
    return data.shape[1], data.shape[0], data


def clear_cache():
    _processed.clear()


def upload_premultiplied_texture(draw_param, index, image_path, force_reload=False):
    abs_path = os.path.abspath(image_path)

    # 检查是否已处理过
    if abs_path in _processed and not force_reload:
        # 直接返回当前纹理 ID（假定仍有效）
        if index < len(draw_param.textures):
            return draw_param.textures[index]
        # 若索引越界，则重新处理
        else:
            print(f"[预乘警告] 索引 {index} 越界，重新处理 {image_path}")

    if not os.path.exists(image_path):
        print(f"[预乘错误] 纹理文件不存在: {image_path}")
        return None

    try:
        width, height, data = _premultiply_image_data(image_path)
    except Exception as e:
        print(f"[预乘错误] 处理纹理失败 {image_path}: {e}")
        return None

    # 检查 draw_param.textures 长度
    if index >= len(draw_param.textures):
        print(f"[预乘错误] 索引 {index} 超出纹理列表长度 {len(draw_param.textures)}")
        return None

    # 删除旧纹理（如果有）
    old_id = draw_param.textures[index]
    if old_id is not None:
        try:
            gl.glDeleteTextures([old_id])
        except Exception as e:
            print(f"[预乘警告] 删除旧纹理失败: {e}")

    # 创建新纹理
    new_id = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, new_id)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA,
                    width, height, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data)

    # 设置纹理参数
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)

    # 生成 Mipmap
    gl.glGenerateMipmap(gl.GL_TEXTURE_2D)

    # 各向异性过滤
    try:
        if gl.glGetString(gl.GL_EXTENSIONS) is not None:
            max_aniso = gl.glGetFloatv(GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT)
            if max_aniso > 16.0:
                max_aniso = 16.0
            gl.glTexParameterf(gl.GL_TEXTURE_2D, GL_TEXTURE_MAX_ANISOTROPY_EXT, max_aniso)
    except Exception:
        pass

    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    # 替换纹理 ID
    draw_param.setTexture(index, new_id)

    # 标记为已处理
    _processed.add(abs_path)

    # 释放 CPU 像素数据（numpy 数组）
    del data

    return new_id


def premultiply_model_textures(model, model_json_dict, model_dir, force_reload=False):
    if model_json_dict is None:
        print("[预乘错误] model_json_dict 为空")
        return 0

    textures = model_json_dict.get("textures", [])
    if not textures:
        print("[预乘错误] 无纹理列表")
        return 0

    if not hasattr(model, 'live2DModel') or not hasattr(model.live2DModel, 'getDrawParam'):
        print("[预乘错误] 无法获取 draw_param")
        return 0

    draw_param = model.live2DModel.getDrawParam()
    if draw_param is None:
        print("[预乘错误] draw_param 为 None")
        return 0

    if len(draw_param.textures) < len(textures):
        print(f"[预乘错误] draw_param.textures 长度 ({len(draw_param.textures)}) 小于纹理列表长度 ({len(textures)})")
        return 0

    success_count = 0
    for i, tex_file in enumerate(textures):
        tex_path = os.path.join(model_dir, tex_file)
        if not os.path.exists(tex_path):
            print(f"[预乘错误] 文件不存在: {tex_path}")
            continue

        new_id = upload_premultiplied_texture(draw_param, i, tex_path, force_reload)
        if new_id is not None:
            success_count += 1

    return success_count
