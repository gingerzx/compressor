#!/usr/bin/env python3
"""
Enhanced 8MB Video Compressor
Optimized two-pass encoding with intelligent quality adjustment
"""
import subprocess, sys, os, json, math, shutil, time
from pathlib import Path
from typing import Optional, Tuple

# Redirect stdin to prevent input() errors when run without console
if sys.platform == "win32":
    sys.stdin = open(os.devnull, 'r')

# === CONFIG ===
TARGET_BYTES = 8 * 1024 * 1024
TARGET_BITS = TARGET_BYTES * 8
AUDIO_BITRATE = 96_000  # Lowered from 128k, imperceptible difference
MIN_VIDEO_BITRATE = 80_000
SAFETY_MARGIN = 0.03  # 3% safety margin
MAX_ATTEMPTS = 3

# === ENCODER PRIORITY ===
ENCODER_PRIORITY = [
    ("hevc_nvenc", "p5"),      # NVIDIA GPU
    ("hevc_qsv", "medium"),    # Intel Quick Sync
    ("libx265", "veryfast")    # CPU fallback
]


class VideoInfo:
    """Container for video metadata"""
    def __init__(self, duration: float, has_audio: bool, width: int, height: int, bitrate: int):
        self.duration = duration
        self.has_audio = has_audio
        self.width = width
        self.height = height
        self.bitrate = bitrate


def locate_tool(name: str) -> Optional[str]:
    """Locate ffmpeg/ffprobe in common locations"""
    # Check same directory as script
    script_dir = Path(sys.executable).parent
    candidate = script_dir / f"{name}.exe"
    if candidate.is_file():
        return str(candidate)
    
    # Check system PATH
    return shutil.which(name) or shutil.which(f"{name}.exe")


def get_video_info(ffprobe: str, input_path: str) -> Optional[VideoInfo]:
    """Extract comprehensive video metadata"""
    try:
        cmd = [
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,duration,bit_rate:format=duration",
            "-of", "json", input_path
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(proc.stdout)
        
        # Get duration
        duration = None
        if "streams" in data and data["streams"] and "duration" in data["streams"][0]:
            duration = float(data["streams"][0]["duration"])
        elif "format" in data and "duration" in data["format"]:
            duration = float(data["format"]["duration"])
        
        if not duration or duration <= 0:
            return None
        
        # Get dimensions
        stream = data["streams"][0]
        width = int(stream.get("width", 1920))
        height = int(stream.get("height", 1080))
        bitrate = int(stream.get("bit_rate", 0))
        
        # Check for audio
        cmd_audio = [ffprobe, "-v", "error", "-select_streams", "a:0",
                     "-show_entries", "stream=codec_type", "-of", "json", input_path]
        proc_audio = subprocess.run(cmd_audio, capture_output=True, text=True)
        has_audio = "streams" in json.loads(proc_audio.stdout) and len(json.loads(proc_audio.stdout)["streams"]) > 0
        
        return VideoInfo(duration, has_audio, width, height, bitrate)
        
    except Exception as e:
        print(f"Error getting video info: {e}")
        return None


def detect_encoder(ffmpeg: str) -> Tuple[str, str]:
    """Detect best available encoder"""
    try:
        proc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                            capture_output=True, text=True)
        available = proc.stdout
        
        for codec, preset in ENCODER_PRIORITY:
            if codec in available:
                print(f"✓ Using {codec} encoder")
                return codec, preset
                
    except Exception:
        pass
    
    return "libx265", "veryfast"  # Ultimate fallback


def calculate_target_bitrate(info: VideoInfo) -> int:
    """Calculate optimal video bitrate"""
    # Reserve space for audio (if present)
    audio_bits = (AUDIO_BITRATE * info.duration) if info.has_audio else 0
    
    # Calculate available bits with safety margin
    available_bits = (TARGET_BITS * (1 - SAFETY_MARGIN)) - audio_bits
    
    # Calculate target video bitrate
    video_bitrate = available_bits / info.duration
    video_bitrate = max(MIN_VIDEO_BITRATE, math.floor(video_bitrate / 1000) * 1000)
    
    return int(video_bitrate)


def get_scale_filter(info: VideoInfo) -> str:
    """Generate smart scaling filter - never upscale"""
    target_w, target_h = 1920, 1080
    
    # Don't upscale if source is smaller
    if info.width <= target_w and info.height <= target_h:
        return "scale=trunc(iw/2)*2:trunc(ih/2)*2"  # Just ensure even dimensions
    
    # Scale down maintaining aspect ratio
    return f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:force_divisible_by=2"


def notify(title: str, message: str):
    """Windows toast notification ONLY"""
    if sys.platform != "win32":
        return
    
    try:
        # Escape for PowerShell
        title_safe = title.replace("'", "''").replace('"', '`"')
        message_safe = message.replace("'", "''").replace('"', '`"')
        
        ps_script = f"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]

$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$toastXml = [xml] $template.GetXml()
$toastXml.GetElementsByTagName("text")[0].AppendChild($toastXml.CreateTextNode('{title_safe}')) | Out-Null
$toastXml.GetElementsByTagName("text")[1].AppendChild($toastXml.CreateTextNode('{message_safe}')) | Out-Null

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($toastXml.OuterXml)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("8MB Compressor")
$notifier.Show($toast)
"""
        
        subprocess.Popen(
            ["powershell", "-WindowStyle", "Hidden", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            creationflags=0x08000000,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL
        )
    except:
        pass


def run_encode(cmd: list, desc: str = "Encoding") -> bool:
    """Execute ffmpeg command silently"""
    try:
        # Use STARTUPINFO to hide console window completely
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
        
        result = subprocess.run(
            cmd,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            startupinfo=startupinfo
        )
        return result.returncode == 0
    except Exception:
        return False


def compress_video(input_path: str, output_path: str, ffmpeg: str, ffprobe: str) -> bool:
    """Main compression routine with iterative quality adjustment"""
    
    # Get video info
    info = get_video_info(ffprobe, input_path)
    if not info:
        notify("❌ Error", "Could not analyze video")
        return False
    
    # Detect encoder
    codec, preset = detect_encoder(ffmpeg)
    
    # Calculate initial bitrate
    video_bitrate = calculate_target_bitrate(info)
    scale_filter = get_scale_filter(info)
    
    # Prepare base command
    base_cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", input_path,
        "-vf", scale_filter,
        "-c:v", codec,
        "-preset", preset
    ]
    
    # Add audio settings
    if info.has_audio:
        base_cmd.extend(["-c:a", "aac", "-b:a", "96k"])
    else:
        base_cmd.extend(["-an"])
    
    # Compression loop
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Build command with current bitrate
        cmd = base_cmd.copy()
        cmd.extend([
            "-b:v", str(video_bitrate),
            "-maxrate", str(int(video_bitrate * 1.5)),
            "-bufsize", str(int(video_bitrate * 2)),
            "-movflags", "+faststart",
            output_path
        ])
        
        # Encode
        if not run_encode(cmd, f"Pass {attempt}"):
            notify("❌ Encoding Failed", f"Could not encode video (attempt {attempt})")
            return False
        
        time.sleep(0.2)
        
        # Check size
        if not os.path.exists(output_path):
            notify("❌ Error", "Output file not created")
            return False
        
        out_size = os.path.getsize(output_path)
        size_mb = out_size / (1024 * 1024)
        
        # Success!
        if out_size <= TARGET_BYTES:
            filename = Path(input_path).name
            notify("✅ Compression Complete!", f"{filename}\nFinal size: {size_mb:.2f} MB")
            return True
        
        # Too big - reduce bitrate
        if attempt < MAX_ATTEMPTS:
            scale_factor = (TARGET_BYTES / out_size) * 0.95
            new_bitrate = max(MIN_VIDEO_BITRATE, int(video_bitrate * scale_factor))
            
            if new_bitrate >= video_bitrate:
                notify("⚠️ Warning", f"Final size: {size_mb:.2f} MB\nCannot reduce further")
                return False
            
            video_bitrate = new_bitrate
    
    # Could not get under 8MB
    notify("⚠️ Warning", f"Final size: {size_mb:.2f} MB\n(Target was 8 MB)")
    return False


def main() -> int:
    # Silent mode - no console output at all
    if len(sys.argv) < 2:
        return 0
    
    input_path = sys.argv[1]
    if not os.path.isfile(input_path):
        notify("❌ Error", "File not found")
        return 1
    
    # Locate tools
    ffmpeg = locate_tool("ffmpeg")
    ffprobe = locate_tool("ffprobe")
    
    if not ffmpeg or not ffprobe:
        notify("❌ Error", "FFmpeg not found. Please reinstall.")
        return 2
    
    # Setup output path
    input_file = Path(input_path)
    output_path = input_file.parent / f"8mb[{input_file.stem}].mp4"
    
    # Show starting notification
    notify("🎬 Starting...", f"Compressing {input_file.name}")
    
    # Compress
    success = compress_video(str(input_path), str(output_path), ffmpeg, ffprobe)
    
    return 0 if success else 3


if __name__ == "__main__":
    sys.exit(main())