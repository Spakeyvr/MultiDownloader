import sys, os, re
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel,
    QPushButton, QLineEdit, QComboBox, QFileDialog, QProgressBar,
    QVBoxLayout, QHBoxLayout, QMessageBox, QCheckBox)
from PySide6.QtCore import Qt, QObject, Signal, QThread, QSettings
from PySide6.QtGui import QIcon

# Format map
format_map = {
    "Best Available": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    "720p": "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]",
    "1080p": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]",
    "1440p": "bv*[height<=1440][ext=mp4]+ba[ext=m4a]/b[height<=1440][ext=mp4]",
    "2160p (4K)": "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/b[height<=2160][ext=mp4]",
    "4320p (8K)": "bv*[height<=4320][ext=mp4]+ba[ext=m4a]/b[height<=4320][ext=mp4]",
    "15360p (16K)": "bv*[height<=15360][ext=mp4]+ba[ext=m4a]/b[height<=15360][ext=mp4]"
}

# Precompiled regex patterns
SUPPORTED_PLATFORMS = {
    "YouTube": [re.compile(r'youtube\.com/watch\?v=', re.I),
                re.compile(r'youtu\.be/', re.I),
                re.compile(r'youtube\.com/playlist\?list=', re.I),
                re.compile(r'youtube\.com/shorts/', re.I)],
    "Instagram": [re.compile(r'instagram\.com/(p|reel|tv|stories)/', re.I)],
    "TikTok": [re.compile(r'tiktok\.com/@[\w\.-]+/video/', re.I),
               re.compile(r'vm\.tiktok\.com/', re.I),
               re.compile(r'tiktok\.com/t/', re.I)],
    "Twitter/X": [re.compile(r'(twitter|x)\.com/[\w]+/status/', re.I)],
    "Facebook": [re.compile(r'facebook\.com/(watch|[\w\.-]+/videos/)', re.I),
                 re.compile(r'fb\.watch/', re.I)],
    "Reddit": [re.compile(r'reddit\.com/r/[\w]+/comments/', re.I)],
    "Twitch": [re.compile(r'twitch\.tv/videos/', re.I),
               re.compile(r'clips\.twitch\.tv/', re.I)]
}

def detect_platform(url):
    for platform, patterns in SUPPORTED_PLATFORMS.items():
        if any(p.search(url) for p in patterns):
            return platform
    return "Unknown"

def validate_url(url):
    return detect_platform(url) != "Unknown"

def hms_to_seconds(t):
    parts = [int(p) for p in t.split(":")]
    if len(parts) == 1: return parts[0]
    if len(parts) == 2: return parts[0]*60 + parts[1]
    if len(parts) == 3: return parts[0]*3600 + parts[1]*60 + parts[2]
    raise ValueError

_nvenc_cache = None
def has_nvenc(ffmpeg_path: str) -> bool:
    global _nvenc_cache
    if _nvenc_cache is not None:
        return _nvenc_cache
    try:
        import subprocess
        out = subprocess.check_output(
            [ffmpeg_path, "-encoders"], stderr=subprocess.STDOUT, text=True
        )
        _nvenc_cache = "h264_nvenc" in out
    except Exception:
        _nvenc_cache = False
    return _nvenc_cache

class DownloadWorker(QObject):
    finished = Signal()
    progress_update = Signal(int)
    status_update = Signal(str)
    platform_detected = Signal(str)

    def __init__(self, url, fmt, path, audio_only, res_path,
                 clip_range, gpu_encode):
        super().__init__()
        self.url, self.fmt, self.path = url, fmt, path
        self.audio_only, self.res_path = audio_only, res_path
        self.clip_range, self.gpu_encode = clip_range, gpu_encode

    def run(self):
        from yt_dlp import YoutubeDL  # lazy import
        platform = detect_platform(self.url)
        self.platform_detected.emit(platform)

        ffmpeg = self.res_path("ffmpeg.exe")

        postproc = ([{'key':'FFmpegExtractAudio','preferredcodec':'mp3','preferredquality':'320'}]
                    if self.audio_only else
                    [{'key':'FFmpegVideoConvertor','preferedformat':'mp4'}])

        extra_ff = []
        if self.gpu_encode and not self.audio_only and has_nvenc(ffmpeg):
            extra_ff = ['-c:v', 'h264_nvenc', '-preset', 'fast', '-crf', '23']
        elif self.gpu_encode:
            self.status_update.emit("NVENC not found, CPU encode")

        outtmpl = os.path.join(self.path, "%(title)s.%(ext)s")
        if platform == "Instagram": outtmpl = os.path.join(self.path, "IG_%(uploader)s_%(title)s.%(ext)s")
        elif platform == "TikTok": outtmpl = os.path.join(self.path, "TT_%(uploader)s_%(title)s.%(ext)s")
        elif platform == "Twitter/X": outtmpl = os.path.join(self.path, "X_%(uploader)s_%(title)s.%(ext)s")

        opts = {
            'format': self.fmt,
            'ffmpeg_location': ffmpeg,
            'merge_output_format': 'mp4',
            'outtmpl': outtmpl,
            'progress_hooks': [self.hook],
            'noplaylist': True,
            'nopart': True,
            'quiet': True,
            'nooverwrites': False,
            'postprocessors': postproc
        }
        if extra_ff and not self.audio_only:
            opts['postprocessor_args'] = {'ffmpeg': extra_ff}

        if self.clip_range and platform == "YouTube":
            s, e = self.clip_range
            if e <= s: s, e = e, s
            def _clip(_info, _ydl): return [{'start_time': s, 'end_time': e}]
            opts['download_ranges'] = _clip
            self.status_update.emit(f"Clipping {s}s -> {e}s")

        try:
            self.status_update.emit(f"Downloading from {platform}...")
            with YoutubeDL(opts) as ydl:
                ydl.download([self.url])
            self.status_update.emit("Done")
        except Exception as ex:
            self.status_update.emit(f"Error: {ex}")
        self.progress_update.emit(100)
        self.finished.emit()

    def hook(self, d):
        if d['status'] == 'downloading':
            tot = d.get('total_bytes') or d.get('total_bytes_estimate')
            got = d.get('downloaded_bytes', 0)
            if tot:
                p = got/tot*100
                self.progress_update.emit(int(p))
                spd = d.get('speed', 0)
                if spd:
                    self.status_update.emit(f"Downloading... {p:.1f}% • {spd/(1024*1024):.1f} MB/s")
                else:
                    self.status_update.emit(f"Downloading... {p:.1f}%")
            else:
                self.status_update.emit("Starting download...")
        elif d['status'] == 'finished':
            self.status_update.emit("Processing...")

class DownloaderWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Multi-Platform Video Downloader")
        self.setMinimumSize(650, 450)
        self.setMaximumWidth(1000)

        self.settings = QSettings("SpakieTools", "MultiDownloader")
        self.download_path = self.settings.value("last_folder", type=str)
        self.start_theme = self.settings.value("theme", "Dark", type=str)

        self.init_ui()
        self.set_dark_mode(self.start_theme == "Dark")

    def save_current_settings(self):
        self.settings.setValue("last_folder", self.download_path or "")
        self.settings.setValue("theme", self.theme.currentText())

    def init_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        L = QVBoxLayout(central)
        L.setSpacing(16)
        L.setContentsMargins(32, 32, 32, 32)

        tL = QHBoxLayout()
        tL.addWidget(QLabel("Theme:"))
        self.theme = QComboBox()
        self.theme.addItems(["Dark","Light"])
        self.theme.setCurrentText(self.start_theme)
        self.theme.currentTextChanged.connect(lambda v: (self.set_dark_mode(v=="Dark"),
                                                         self.save_current_settings()))
        tL.addWidget(self.theme)
        L.addLayout(tL)

        self.platform_info = QLabel("Supported: YouTube, Instagram, TikTok, Twitter/X, Facebook, Reddit, Twitch")
        self.platform_info.setStyleSheet("font-size:11px;color:gray;margin-bottom:8px;")
        L.addWidget(self.platform_info)

        self.url_in = QLineEdit()
        self.url_in.setPlaceholderText("Paste video URL from any supported platform…")
        self.url_in.textChanged.connect(self.on_url_changed)
        L.addWidget(self.url_in)

        self.platform_label = QLabel("Platform: None detected")
        self.platform_label.setStyleSheet("font-size:12px;color:#00b894;margin:4px 0;")
        L.addWidget(self.platform_label)

        self.fmt_box = QComboBox()
        self.fmt_box.addItems(format_map.keys())
        self.fmt_box.setCurrentText("1080p")
        L.addWidget(self.fmt_box)

        self.audio_cb = QCheckBox("Audio only (MP3)")
        L.addWidget(self.audio_cb)

        self.gpu_cb = QCheckBox("GPU re-encode (NVENC) - YouTube only")
        L.addWidget(self.gpu_cb)

        self.range_in = QLineEdit()
        self.range_in.setPlaceholderText("Clip range e.g. 1:45 1:55 (YouTube only), leave blank for entire video")
        L.addWidget(self.range_in)

        self.go = QPushButton("Download")
        self.go.setCursor(Qt.PointingHandCursor)
        self.go.clicked.connect(self.start)
        L.addWidget(self.go)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)  # smoother default
        self.bar.setTextVisible(True)
        L.addWidget(self.bar)

        self.status = QLabel()
        L.addWidget(self.status)

        fL = QVBoxLayout()
        self.pick = QPushButton("Choose Download Folder")
        self.pick.setCursor(Qt.PointingHandCursor)
        self.pick.clicked.connect(self.pick_folder)
        fL.addWidget(self.pick)

        self.open_folder = QPushButton("Open Folder")
        self.open_folder.setCursor(Qt.PointingHandCursor)
        self.open_folder.clicked.connect(self.open_download_folder)
        fL.addWidget(self.open_folder)

        self.folder_lbl = QLabel(f"Save to: {self.download_path}" if self.download_path else "No folder selected")
        self.folder_lbl.setStyleSheet("font-size:11px;color:gray;margin-top:2px;")
        fL.addWidget(self.folder_lbl)
        L.addLayout(fL)

    def on_url_changed(self, text):
        if text.strip():
            platform = detect_platform(text.strip())
            if platform != "Unknown":
                self.platform_label.setText(f"Platform: {platform} ✓")
                self.platform_label.setStyleSheet("font-size:12px;color:#00b894;margin:4px 0;")
            else:
                self.platform_label.setText("Platform: Not supported ✗")
                self.platform_label.setStyleSheet("font-size:12px;color:#e74c3c;margin:4px 0;")
        else:
            self.platform_label.setText("Platform: None detected")
            self.platform_label.setStyleSheet("font-size:12px;color:gray;margin:4px 0;")

    def set_dark_mode(self, on):
        bg_color = "#1e1e1e" if on else "#ffffff"
        text_color = "#f0f0f0" if on else "#000000"
        input_bg = "#2a2a2a" if on else "#f5f5f5"
        input_border = "#444444" if on else "#dddddd"
        button_hover = "#3a3a3a" if on else "#e0e0e0"
        progress_bg = "#2a2a2a" if on else "#f5f5f5"
        progress_border = "#444444" if on else "#dddddd"

        self.setStyleSheet(f"""
            QWidget {{
                background-color: {bg_color};
                color: {text_color};
                font-family: 'Segoe UI', sans-serif;
                font-size: 13px;
            }}
            QLineEdit, QComboBox {{
                background-color: {input_bg};
                color: {text_color};
                border: 1px solid {input_border};
                padding: 10px;
                border-radius: 8px;
            }}
            QComboBox::drop-down {{
                border: none;
            }}
            QPushButton {{
                background-color: {input_bg};
                color: {text_color};
                border: 1px solid {input_border};
                padding: 10px;
                border-radius: 8px;
            }}
            QPushButton:hover {{
                background-color: {button_hover};
            }}
            QProgressBar {{
                background-color: {progress_bg};
                border: 1px solid {progress_border};
                border-radius: 8px;
                text-align: center;
                height: 20px;
            }}
            QProgressBar::chunk {{
                background-color: #00b894;
                border-radius: 8px;
            }}
            QCheckBox {{
                spacing: 8px;
            }}
            QLabel {{
                color: {text_color};
            }}
        """)

    def pick_folder(self):
        p = QFileDialog.getExistingDirectory(self,"Choose Download Folder")
        if p:
            self.download_path = p
            self.folder_lbl.setText(f"Save to: {p}")
            self.save_current_settings()
        else:
            QMessageBox.warning(self,"No folder","Pick a save folder.")

    def open_download_folder(self):
        if self.download_path and os.path.exists(self.download_path):
            os.startfile(self.download_path)
        else:
            QMessageBox.warning(self, "No Folder", "No download folder selected or it doesn't exist.")

    def start(self):
        url = self.url_in.text().strip()
        if not url:
            return QMessageBox.warning(self,"No URL","Paste a video URL from any supported platform.")

        if not validate_url(url):
            return QMessageBox.warning(self,"Unsupported URL","This platform is not supported. Check the supported platforms list.")

        if not self.download_path:
            return QMessageBox.warning(self,"No folder","Pick a save folder.")

        platform = detect_platform(url)

        clip = None
        txt = self.range_in.text().strip()
        if txt:
            if platform != "YouTube":
                return QMessageBox.warning(self,"Clipping Not Supported",f"Video clipping is only supported for YouTube, not {platform}.")
            parts = re.split(r"\s+", txt)
            if len(parts) != 2:
                return QMessageBox.warning(self,"Range","Use: start end (space-separated).")
            try:
                a, b = hms_to_seconds(parts[0]), hms_to_seconds(parts[1])
                if a == b:
                    return QMessageBox.warning(self,"Range","Start and end times cannot be the same.")
                clip = (a, b)
            except ValueError:
                return QMessageBox.warning(self,"Range","Invalid time format. Use seconds or MM:SS or HH:MM:SS.")

        if self.audio_cb.isChecked():
            fmt = "bestaudio[ext=m4a]/bestaudio/best"
        else:
            if platform in ["TikTok", "Twitter/X", "Instagram"]:
                fmt = "best[ext=mp4]/best"
            else:
                fmt = format_map[self.fmt_box.currentText()]

        self.bar.setRange(0, 0)
        self.status.setText("Starting...")
        self.go.setEnabled(False)

        self.thread = QThread()
        self.worker = DownloadWorker(
            url, fmt, self.download_path, self.audio_cb.isChecked(),
            self.resource_path, clip, self.gpu_cb.isChecked()
        )

        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.done)
        self.worker.progress_update.connect(self.bar.setValue)
        self.worker.status_update.connect(self.status.setText)
        self.worker.platform_detected.connect(self.on_platform_detected)

        self.thread.start()

    def on_platform_detected(self, platform):
        self.platform_label.setText(f"Downloading from: {platform}")

    def done(self):
        self.go.setEnabled(True)
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.thread.quit()
        self.thread.wait()
        self.worker.deleteLater()
        self.thread.deleteLater()

    def resource_path(self, rel):
        try:
            base = sys._MEIPASS  # type: ignore
        except Exception:
            base = os.path.abspath(".")
        return os.path.join(base, rel)

    def closeEvent(self, e):
        self.save_current_settings()
        e.accept()

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    win = DownloaderWindow()
    app.setWindowIcon(QIcon(win.resource_path("yt_downloader_icon.ico")))
    win.show()
    app.exec()