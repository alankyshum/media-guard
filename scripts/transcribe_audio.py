#!/usr/bin/env python3
"""
Audio transcription script using OpenAI Whisper
Supports multiple audio formats and generates subtitle files (SRT/VTT)
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Dict, List


def transcribe(
    path: str,
    model_size: Optional[str] = None,
    language: Optional[str] = None,
    task: str = "transcribe",
    backend: str = "auto"
) -> Dict:
    """
    Transcription helper with backend selection (mlx or openai).
    Returns a dict with {"text": str, "segments": [{"start": float, "end": float, "text": str}, ...], "language": str}
    """
    # Determine what is available
    mlx_available = False
    try:
        import mlx_whisper
        mlx_available = True
    except ImportError:
        pass

    openai_available = False
    try:
        import whisper
        openai_available = True
    except ImportError:
        pass

    if not mlx_available and not openai_available:
        print("Error: Neither mlx_whisper nor openai-whisper package is installed.", file=sys.stderr)
        print("Install with: scripts/setup.sh (or uv pip install --python <venv>/bin/python mlx-whisper|openai-whisper).", file=sys.stderr)
        sys.exit(1)

    if backend == "mlx" and not mlx_available:
        print("Error: mlx_whisper package is not installed.", file=sys.stderr)
        print("Install with: scripts/setup.sh (or uv pip install --python <venv>/bin/python mlx-whisper).", file=sys.stderr)
        sys.exit(1)

    if backend == "openai" and not openai_available:
        print("Error: whisper package is not installed.", file=sys.stderr)
        print("Install with: scripts/setup.sh (or uv pip install --python <venv>/bin/python openai-whisper).", file=sys.stderr)
        sys.exit(1)

    model_name = model_size if model_size else "base"

    # Try MLX backend
    if backend in ("auto", "mlx") and mlx_available:
        if "/" in model_name or model_name.startswith("mlx-community/"):
            repo = model_name
        else:
            mlx_mapping = {
                "tiny": "mlx-community/whisper-tiny",
                "base": "mlx-community/whisper-base-mlx",
                "small": "mlx-community/whisper-small-mlx",
                "medium": "mlx-community/whisper-medium-mlx",
                "large": "mlx-community/whisper-large-v3-mlx",
                "turbo": "mlx-community/whisper-large-v3-turbo",
            }
            repo = mlx_mapping.get(model_name, "mlx-community/whisper-large-v3-turbo")

        print(f"[transcribe] backend=mlx repo={repo}", file=sys.stderr)
        try:
            import mlx_whisper
            kwargs = {}
            if language:
                kwargs["language"] = language
            if task:
                kwargs["task"] = task

            res = mlx_whisper.transcribe(
                path,
                path_or_hf_repo=repo,
                **kwargs
            )
            return {
                "text": res.get("text", ""),
                "segments": [
                    {
                        "start": float(seg.get("start", 0.0)),
                        "end": float(seg.get("end", 0.0)),
                        "text": seg.get("text", "")
                    }
                    for seg in res.get("segments", [])
                ],
                "language": res.get("language", "")
            }
        except Exception as e:
            if backend == "mlx":
                raise e
            print(f"Warning: MLX transcription failed: {e}. Falling back to OpenAI Whisper.", file=sys.stderr)

    # Try OpenAI Whisper backend
    openai_model = model_name
    if openai_model == "turbo":
        openai_model = "large"
    elif "/" in openai_model:
        if "tiny" in openai_model:
            openai_model = "tiny"
        elif "base" in openai_model:
            openai_model = "base"
        elif "small" in openai_model:
            openai_model = "small"
        elif "medium" in openai_model:
            openai_model = "medium"
        elif "large" in openai_model:
            openai_model = "large"
        elif "turbo" in openai_model:
            openai_model = "large"
        else:
            openai_model = "base"

    print(f"[transcribe] backend=openai model={openai_model}", file=sys.stderr)
    import whisper
    model = whisper.load_model(openai_model)
    res = model.transcribe(
        path,
        language=language,
        task=task,
        verbose=False
    )
    return {
        "text": res.get("text", ""),
        "segments": [
            {
                "start": float(seg.get("start", 0.0)),
                "end": float(seg.get("end", 0.0)),
                "text": seg.get("text", "")
            }
            for seg in res.get("segments", [])
        ],
        "language": res.get("language", "")
    }


def format_timestamp(seconds: float, subtitle_format: str = "srt") -> str:
    """Format timestamp for subtitle files"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    
    if subtitle_format == "srt":
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    else:  # vtt
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def generate_srt(segments: List[Dict], output_path: str) -> None:
    """Generate SRT subtitle file"""
    with open(output_path, 'w', encoding='utf-8') as f:
        for i, segment in enumerate(segments, 1):
            start = format_timestamp(segment['start'], 'srt')
            end = format_timestamp(segment['end'], 'srt')
            text = segment['text'].strip()
            
            f.write(f"{i}\n")
            f.write(f"{start} --> {end}\n")
            f.write(f"{text}\n\n")


def generate_vtt(segments: List[Dict], output_path: str) -> None:
    """Generate VTT subtitle file"""
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("WEBVTT\n\n")
        
        for segment in segments:
            start = format_timestamp(segment['start'], 'vtt')
            end = format_timestamp(segment['end'], 'vtt')
            text = segment['text'].strip()
            
            f.write(f"{start} --> {end}\n")
            f.write(f"{text}\n\n")


def generate_txt(segments: List[Dict], output_path: str) -> None:
    """Generate plain text transcript"""
    with open(output_path, 'w', encoding='utf-8') as f:
        for segment in segments:
            f.write(segment['text'].strip() + " ")


def generate_json(result: Dict, output_path: str) -> None:
    """Generate JSON with full transcription data"""
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)


def transcribe_audio(
    audio_path: str,
    model_size: Optional[str] = None,
    language: Optional[str] = None,
    output_dir: Optional[str] = None,
    formats: List[str] = ["srt"],
    task: str = "transcribe",
    backend: str = "auto"
) -> Dict:
    """
    Transcribe audio file using Whisper
    
    Args:
        audio_path: Path to audio file
        model_size: Whisper model size (tiny, base, small, medium, large)
        language: Language code (e.g., 'en', 'es', 'fr') or None for auto-detect
        output_dir: Directory for output files (defaults to audio file directory)
        formats: List of output formats (srt, vtt, txt, json)
        task: 'transcribe' or 'translate' (translate to English)
        backend: Transcription backend ('auto', 'mlx', or 'openai')
    
    Returns:
        Dictionary with transcription results
    """
    # Validate inputs
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    
    audio_path = Path(audio_path)
    
    if output_dir is None:
        output_dir = audio_path.parent
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    
    # Base filename for outputs
    base_name = audio_path.stem
    
    print(f"Transcribing audio: {audio_path.name}...")
    result = transcribe(
        path=str(audio_path),
        model_size=model_size,
        language=language,
        task=task,
        backend=backend
    )
    
    # Generate requested output formats
    output_files = {}
    
    if "srt" in formats:
        srt_path = output_dir / f"{base_name}.srt"
        generate_srt(result['segments'], str(srt_path))
        output_files['srt'] = str(srt_path)
        print(f"✓ Generated SRT: {srt_path}")
    
    if "vtt" in formats:
        vtt_path = output_dir / f"{base_name}.vtt"
        generate_vtt(result['segments'], str(vtt_path))
        output_files['vtt'] = str(vtt_path)
        print(f"✓ Generated VTT: {vtt_path}")
    
    if "txt" in formats:
        txt_path = output_dir / f"{base_name}.txt"
        generate_txt(result['segments'], str(txt_path))
        output_files['txt'] = str(txt_path)
        print(f"✓ Generated TXT: {txt_path}")
    
    if "json" in formats:
        json_path = output_dir / f"{base_name}.json"
        generate_json(result, str(json_path))
        output_files['json'] = str(json_path)
        print(f"✓ Generated JSON: {json_path}")
    
    print(f"\n✅ Transcription complete!")
    print(f"Detected language: {result['language']}")
    print(f"Full text: {result['text'][:100]}...")
    
    return {
        'text': result['text'],
        'language': result['language'],
        'segments': result['segments'],
        'output_files': output_files
    }


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe audio to subtitles using Whisper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic transcription (SRT output)
  python transcribe_audio.py audio.mp3
  
  # Multiple formats
  python transcribe_audio.py audio.mp3 --formats srt vtt txt json
  
  # Specify language and model
  python transcribe_audio.py audio.mp3 --language en --model small
  
  # Translate to English
  python transcribe_audio.py audio.mp3 --task translate
  
  # Custom output directory
  python transcribe_audio.py audio.mp3 --output-dir ./transcripts
        """
    )
    
    parser.add_argument(
        "audio_path",
        help="Path to audio file (supports mp3, wav, m4a, flac, etc.)"
    )
    
    parser.add_argument(
        "--model",
        default="base",
        help="Whisper model size or MLX repository (e.g. tiny, base, small, medium, large, turbo or custom MLX HF repo; default: base)"
    )
    
    parser.add_argument(
        "--backend",
        choices=["auto", "mlx", "openai"],
        default="auto",
        help="Transcription backend (default: auto)"
    )
    
    parser.add_argument(
        "--language",
        help="Language code (e.g., 'en', 'es', 'fr'). Auto-detect if not specified"
    )
    
    parser.add_argument(
        "--output-dir",
        help="Output directory for subtitle files (default: same as audio file)"
    )
    
    parser.add_argument(
        "--formats",
        nargs="+",
        choices=["srt", "vtt", "txt", "json"],
        default=["srt"],
        help="Output formats (default: srt)"
    )
    
    parser.add_argument(
        "--task",
        choices=["transcribe", "translate"],
        default="transcribe",
        help="Task: transcribe in original language or translate to English"
    )
    
    args = parser.parse_args()
    
    try:
        result = transcribe_audio(
            audio_path=args.audio_path,
            model_size=args.model,
            language=args.language,
            output_dir=args.output_dir,
            formats=args.formats,
            task=args.task,
            backend=args.backend
        )
        
        return 0
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
