"""CPU-only media preparation, isolated in the installed PyAV media runtime."""
from pathlib import Path
import hashlib
import json
import sys

IMAGES = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}
VIDEOS = {'.mp4', '.mkv', '.webm', '.mov', '.avi'}

def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def prepare(request):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = request['limits']['max_pixels']
    source = Path(request['path'])
    if digest(source) != request['sha256']: raise ValueError('input_integrity')
    destination = Path(request['output'])
    destination.mkdir(parents=True, exist_ok=True)
    bounds = request['limits']
    frames = []

    def save(image, timing):
        if image.width * image.height > bounds['max_pixels']: raise ValueError('pixel_budget')
        width, height = image.size
        image = ImageOps.exif_transpose(image).convert('RGB')
        image.thumbnail((768, 768))
        path = destination / f'frame-{len(frames):04d}.png'
        image.save(path)
        frames.append({'path':str(path),'sha256':digest(path),'bytes':path.stat().st_size,
                       'width':image.width,'height':image.height,'original_width':width,'original_height':height,**timing})

    if source.suffix.lower() in IMAGES:
        with Image.open(source) as image:
            save(image, {'timecode_seconds':None,'pts':None,'time_base':None,'frame_index':None})
            count = getattr(image,'n_frames',1)
        return {'frames':frames,'sampling':{'kind':'still_image','complete':count==1,'animated_frames_included':False,'source_image_frames':count}}
    import av
    count = 0
    next_time = 0.0
    reason = 'end_of_video'
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        if stream.codec_context.width * stream.codec_context.height > bounds['max_pixels']: raise ValueError('pixel_budget')
        stream.thread_type = 'NONE'
        duration = float(stream.duration * stream.time_base) if stream.duration is not None else None
        for index, frame in enumerate(container.decode(stream)):
            if frame.pts is None: raise ValueError('missing_frame_pts')
            seconds = float(frame.pts * frame.time_base)
            origin = float((stream.start_time or 0) * stream.time_base)
            elapsed = seconds - origin
            if elapsed >= bounds['max_duration_seconds']:
                reason = 'duration_budget'; break
            if elapsed + 1e-9 < next_time: continue
            if frame.width * frame.height > bounds['max_pixels']: raise ValueError('pixel_budget')
            save(frame.to_image(), {'timecode_seconds':seconds,'elapsed_seconds':elapsed,'pts':frame.pts,
                'time_base':{'numerator':frame.time_base.numerator,'denominator':frame.time_base.denominator},'frame_index':index})
            count += 1
            if count >= bounds['max_frames']:
                reason = 'frame_budget'; break
            next_time = elapsed + bounds['sample_interval_seconds']
    if not frames: raise ValueError('no_video_frames')
    return {'frames':frames,'sampling':{'kind':'sampled_video_frames','complete':False,'reason':reason,
            'duration_seconds':duration,'interval_seconds':bounds['sample_interval_seconds'],
            'audio_analyzed':False,'continuous_motion_analyzed':False}}

if __name__ == '__main__':
    result = prepare(json.load(sys.stdin))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
