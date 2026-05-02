<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
</head>
<body>

<h1>📺 Kisskh Downloader</h1>

<p>
A simple and lightweight command-line tool for downloading dramas and TV series from <b>Kisskh</b> with episode selection, quality control, and subtitle support.
</p>

<div class="box">
<h2>🚀 Features</h2>
<ul>
  <li>Search and download dramas by name or URL</li>
  <li>Download full series or specific episodes</li>
  <li>Select video quality (360p to 1080p)</li>
  <li>Download subtitles (multi-language support)</li>
  <li>Custom output directory</li>
  <li>Fast CLI-based downloader</li>
  <li>Auto fallback quality support</li>
</ul>
</div>

<div class="box">
<h2>📦 Installation</h2>
<pre>pip install -U kisskh-downloader</pre>
</div>

<div class="box">
<h2>🛠️ Requirements</h2>
<ul>
  <li>Python 3.8+</li>
  <li>FFmpeg (optional but recommended)</li>
</ul>

<h3>Install FFmpeg</h3>
<b>Windows:</b>
<p>https://ffmpeg.org/download.html</p>

<b>Linux:</b>
<pre>sudo apt install ffmpeg</pre>

<b>Mac:</b>
<pre>brew install ffmpeg</pre>
</div>

<div class="box">
<h2>📖 Usage</h2>

<h3>🔗 Download using URL</h3>
<pre>kisskh dl "https://kisskh.co/Drama/Example-Drama?id=1234" -o downloads</pre>

<h3>🔍 Search by name</h3>
<pre>kisskh dl "Alchemy of Souls" -o downloads</pre>

<h3>🎬 Download specific episodes</h3>
<pre>kisskh dl "Alchemy of Souls" -f 1 -l 10 -o downloads</pre>

<h3>🎥 Set quality</h3>
<pre>kisskh dl "Alchemy of Souls" -q 720p -o downloads</pre>

<p>Available: 360p | 480p | 540p | 720p | 1080p</p>

<h3>📝 Subtitles</h3>
<pre>kisskh dl "Alchemy of Souls" -s en,th -o downloads</pre>
</div>

<div class="box">
<h2>📂 Output Structure</h2>
<pre>
downloads/
 ├── Drama Name/
 │    ├── Episode 01.mp4
 │    ├── Episode 02.mp4
 │    └── subtitles/
</pre>
</div>

<div class="box">
<h2>🧠 How it works</h2>
<ol>
  <li>User inputs drama name or URL</li>
  <li>Fetch available episodes</li>
  <li>Download video stream</li>
  <li>Optional FFmpeg processing</li>
  <li>Save organized output</li>
</ol>
</div>

<div class="box">
<h2>👨‍💻 Author</h2>
<p><b>Vance Pedroso</b></p>
<p>GitHub: https://github.com/vancepedroso</p>
</div>

<div class="box">
<h2>📜 License</h2>
<p>This project is for educational purposes only.</p>
</div>

</body>
</html>
