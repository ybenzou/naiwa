"""Nearest-neighbour composition of complete opaque actor images."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform
from naiwa.sprite_defs import *
from naiwa.pixel_frame import PixelFrame

PALETTE_INDEX = {p:i for i,p in enumerate(sorted(PALETTE))}
BYTE_INDEX = {bytes(p):i for p,i in PALETTE_INDEX.items()}


def bitmap(frame):
    if isinstance(frame,QImage):return frame
    if hasattr(frame,"indices"):
        image = QImage(frame.indices,len(frame[0]),len(frame),len(frame[0]),QImage.Format.Format_Indexed8)
        from PySide6.QtGui import QColor
        image.setColorTable([QColor(*p).rgba() for p in sorted(PALETTE)])
        return image.convertToFormat(QImage.Format.Format_RGBA8888)
    raw = b"".join(bytes(p) for row in frame for p in row)
    return QImage(raw,len(frame[0]),len(frame),len(frame[0])*4,QImage.Format.Format_RGBA8888).copy()


def pixels(image):
    image=image.convertToFormat(QImage.Format.Format_RGBA8888)
    data,stride=bytes(image.constBits()),image.bytesPerLine()
    indices=bytes(BYTE_INDEX[data[y*stride+x*4:y*stride+x*4+4]]
                  for y in range(image.height()) for x in range(image.width()))
    return PixelFrame(indices,image.width())


def transformed(layer, *, pivot=(80,130), angle=0, scale=1, dx=0, dy=0):
    image=bitmap(layer)
    transform=QTransform().translate(dx,dy).translate(*pivot).rotate(angle).scale(scale,scale).translate(-pivot[0],-pivot[1])
    matrix=QImage.trueMatrix(transform,image.width(),image.height())
    origin=transform.map(QPointF(0,0))-matrix.map(QPointF(0,0))
    result=image.transformed(transform,Qt.TransformationMode.FastTransformation)
    return result,(round(origin.x()),round(origin.y()))


def composite(base, draws):
    image=bitmap(base).copy()
    painter=QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,False)
    for layer,params in draws:
        if params.get("scale",1)<=.001:continue
        part,(x,y)=transformed(layer,**params)
        painter.drawImage(x,y,part)
    painter.end()
    return pixels(image)


def rigid_between(a,b,step,total):
    """Whole-body affine sampling of ONE key texture, never a mixed pixel mesh."""
    if step<=0:return a
    if step>=total:return b
    from naiwa.motion import _bounds
    spans_a,ta,ba=_bounds(a);spans_b,tb,bb=_bounds(b)
    la=min(s[0] for s in spans_a if s);ra=max(s[1] for s in spans_a if s)
    lb=min(s[0] for s in spans_b if s);rb=max(s[1] for s in spans_b if s)
    t=step/total
    source=a if t<.5 else b
    box=(la,ta,ra,ba) if t<.5 else (lb,tb,rb,bb)
    l,top,r,bottom=box
    target=(la*(1-t)+lb*t,ta*(1-t)+tb*t,ra*(1-t)+rb*t,ba*(1-t)+bb*t)
    transform=QTransform().translate(target[0],target[1]).scale((target[2]-target[0])/max(1,r-l),(target[3]-target[1])/max(1,bottom-top)).translate(-l,-top)
    image=bitmap(source)
    result=QImage(len(a[0]),HEIGHT,QImage.Format.Format_RGBA8888);result.fill(0)
    painter=QPainter(result);painter.setWorldTransform(transform);painter.drawImage(0,0,image);painter.end()
    return pixels(result)
