"""
交互式图片查看器
===============
两阶段图片展示：
  1. 缩略图模式：固定宽度缩略图，点击后进入全屏
  2. 全屏模式：突破 Streamlit iframe，覆盖整个网页，支持滚轮缩放、拖拽平移、双击重置
无外部CDN依赖，图片base64内嵌，完全离线可用。
"""

import base64
import streamlit.components.v1 as components


def render_interactive_image(
    img_bytes: bytes,
    width: int = 450,
    key: str = "",
) -> None:
    """
    渲染可点击缩略图，点击后全屏查看（支持缩放拖拽）

    参数:
        img_bytes: PNG/JPEG 图片字节流
        width:     缩略图宽度（px），默认450
        key:       唯一标识
    """
    b64 = base64.b64encode(img_bytes).decode("utf-8")
    # 自动检测图片格式（JPEG/PNG header）
    mime = "image/jpeg" if img_bytes[:3] == b'\xff\xd8\xff' else "image/png"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<style>
    .thumb-wrap-{key} {{
        display: inline-block; cursor: zoom-in; position: relative;
        border: 1px solid #ddd; border-radius: 4px; overflow: hidden;
        max-width: {width}px; line-height: 0;
    }}
    .thumb-wrap-{key}:hover {{
        box-shadow: 0 4px 16px rgba(0,0,0,0.2);
    }}
    .thumb-wrap-{key}:hover .thumb-overlay-{key} {{ opacity: 1; }}
    .thumb-img-{key} {{
        display: block; max-height: 200px; width: 100%; object-fit: cover;
        pointer-events: none; user-select: none; -webkit-user-select: none;
        -webkit-user-drag: none; -webkit-touch-callout: none;
    }}
    .thumb-overlay-{key} {{
        position: absolute; top: 0; left: 0; right: 0; bottom: 0;
        background: rgba(0,0,0,0.25); opacity: 0; transition: opacity 0.2s;
        display: flex; align-items: center; justify-content: center;
        color: white; font-size: 28px; font-family: sans-serif;
        pointer-events: none;
    }}
</style>
</head><body style="margin:0;">
<div class="thumb-wrap-{key}" id="thumb-{key}">
    <img class="thumb-img-{key}" src="data:{mime};base64,{b64}" />
    <div class="thumb-overlay-{key}">🔍</div>
</div>
<script>
(function() {{
    var thumb = document.getElementById('thumb-{key}');
    var b64 = "{b64}";
    var mime = "{mime}";
    var key = "{key}";
    var parentDoc = window.parent.document;
    var parentBody = parentDoc.body;
    var parentWin = window.parent;

    var MIN_Z = 0.5, MAX_Z = 5.0;
    var zoom = 1, panX = 0, panY = 0, fitZoom = 1;
    var dragging = false, mx = 0, my = 0;

    function createFullscreen() {{
        // 遮罩
        var overlay = parentDoc.createElement('div');
        overlay.id = 'fso-parent-' + key;
        overlay.style.cssText = 'position:fixed;top:0;left:0;width:100vw;height:100vh;' +
            'background:rgba(0,0,0,0.92);z-index:999999;display:flex;' +
            'flex-direction:column;align-items:center;justify-content:center;';

        // 关闭按钮
        var closeBtn = parentDoc.createElement('span');
        closeBtn.textContent = '×';
        closeBtn.style.cssText = 'position:absolute;top:16px;right:24px;font-size:36px;' +
            'color:white;cursor:pointer;z-index:1000001;font-family:sans-serif;line-height:1;';
        closeBtn.onclick = destroyFullscreen;

        // 查看器容器
        var viewer = parentDoc.createElement('div');
        viewer.id = 'fsv-parent-' + key;
        viewer.style.cssText = 'width:90vw;height:85vh;overflow:hidden;position:relative;' +
            'cursor:grab;background:#1a1a1a;border:1px solid #444;';

        // 图片
        var img = parentDoc.createElement('img');
        img.src = 'data:' + mime + ';base64,' + b64;
        img.id = 'fsi-parent-' + key;
        img.style.cssText = 'position:absolute;top:0;left:0;transform-origin:0 0;' +
            'user-select:none;pointer-events:none;';

        // 提示文字
        var hint = parentDoc.createElement('div');
        hint.textContent = '适应屏幕';
        hint.style.cssText = 'position:absolute;bottom:6px;right:10px;font-size:11px;' +
            'color:#aaa;font-family:sans-serif;pointer-events:none;' +
            'background:rgba(0,0,0,0.6);padding:3px 8px;border-radius:3px;';

        viewer.appendChild(img);
        viewer.appendChild(hint);
        overlay.appendChild(closeBtn);
        overlay.appendChild(viewer);
        parentBody.appendChild(overlay);

        return {{ overlay: overlay, viewer: viewer, img: img, hint: hint }};
    }}

    var fs = null;

    function destroyFullscreen() {{
        if (fs) {{
            fs.overlay.remove();
            fs = null;
        }}
    }}

    function calcFit() {{
        var vw = fs.viewer.clientWidth, vh = fs.viewer.clientHeight;
        var iw = fs.img.naturalWidth || fs.img.width || 1;
        var ih = fs.img.naturalHeight || fs.img.height || 1;
        return Math.min(vw / iw, vh / ih, 1.5);
    }}

    function updateView() {{
        fs.img.style.transform = 'scale(' + zoom + ') translate(' + panX + 'px, ' + panY + 'px)';
        var pct = Math.round(zoom * 100);
        fs.hint.textContent = pct + '% | 滚轮缩放 · 拖拽平移 · 双击重置 · Esc关闭';
    }}

    function onWheel(e) {{
        e.preventDefault();
        var rect = fs.viewer.getBoundingClientRect();
        var wx = e.clientX - rect.left, wy = e.clientY - rect.top;
        var oldZoom = zoom;
        zoom = Math.min(MAX_Z, Math.max(MIN_Z, zoom + (e.deltaY > 0 ? -0.05 : 0.05)));
        panX = panX + wx / zoom - wx / oldZoom;
        panY = panY + wy / zoom - wy / oldZoom;
        updateView();
    }}

    function onMouseDown(e) {{
        dragging = true; mx = e.clientX; my = e.clientY;
        fs.viewer.style.cursor = 'grabbing';
    }}

    function onMouseMove(e) {{
        if (!dragging) return;
        panX += (e.clientX - mx) / zoom;
        panY += (e.clientY - my) / zoom;
        mx = e.clientX; my = e.clientY;
        updateView();
    }}

    function onMouseUp() {{
        dragging = false;
        if (fs) fs.viewer.style.cursor = 'grab';
    }}

    function onDblClick() {{
        zoom = fitZoom; panX = 0; panY = 0; updateView();
    }}

    function onKeyDown(e) {{
        if (e.key === 'Escape') destroyFullscreen();
    }}

    function showFullscreen() {{
        fs = createFullscreen();
        requestAnimationFrame(function() {{
            fitZoom = calcFit();
            zoom = fitZoom; panX = 0; panY = 0; updateView();

            // 事件绑定
            fs.viewer.addEventListener('wheel', onWheel, {{passive: false}});
            fs.viewer.addEventListener('mousedown', onMouseDown);
            parentWin.addEventListener('mousemove', onMouseMove);
            parentWin.addEventListener('mouseup', onMouseUp);
            fs.viewer.addEventListener('dblclick', onDblClick);
            parentWin.addEventListener('keydown', onKeyDown);
            fs.viewer.addEventListener('contextmenu', function(e) {{ e.preventDefault(); }});
            fs.overlay.addEventListener('click', function(e) {{
                if (e.target === fs.overlay) destroyFullscreen();
            }});

            // 窗口大小变化时重新适配
            var resizeObs = new parentWin.ResizeObserver(function() {{
                if (fs) {{
                    fitZoom = calcFit();
                    zoom = fitZoom; panX = 0; panY = 0; updateView();
                }}
            }});
            resizeObs.observe(fs.viewer);
        }});
    }}

    thumb.addEventListener('click', showFullscreen);
}})();
</script>
</body></html>"""

    components.html(html, height=220, scrolling=False)
