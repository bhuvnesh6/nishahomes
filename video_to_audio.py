from moviepy import VideoFileClip

def extract_audio_from_video(video_path, output_path):
    video = VideoFileClip(video_path)

    if video.audio is None:
        raise Exception("No audio track found in video")

    video.audio.write_audiofile(output_path)

    video.close()