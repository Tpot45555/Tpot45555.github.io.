# Censo completo del PC para "Atlas de mi PC".
# Solo lee nombres, tamanos y fechas. No abre ni modifica ningun archivo.
# Resultado: ciudad_datos.json junto a este script.
$ErrorActionPreference = 'SilentlyContinue'
$root  = $env:USERPROFILE
$out   = Join-Path $PSScriptRoot 'ciudad_datos.json'
$epoch = [datetime]'1970-01-01'

$groups = @{
  code  = 'py js ts tsx jsx html css c cpp h hpp cs java kt rs go rb php sh bat ps1 ipynb lua gd sql vue svelte m scl st'
  img   = 'png jpg jpeg gif bmp webp svg psd tif tiff ico heic raw clip kra xcf ai'
  video = 'mp4 mkv avi mov webm wmv flv m4v'
  audio = 'mp3 wav flac ogg m4a aac opus mid'
  doc   = 'pdf doc docx xls xlsx ppt pptx txt md odt rtf csv tex epub'
  zip   = 'zip rar 7z tar gz bz2 xz iso jar'
  exe   = 'exe dll msi sys bin so apk pak'
  data  = 'json xml yaml yml db sqlite dat log ini cfg toml npy pt pth onnx safetensors ckpt h5 parquet pkl gguf'
}
$map = @{}
foreach ($g in $groups.Keys) { foreach ($e in $groups[$g].Split(' ')) { $map['.' + $e] = $g } }

function Walk([string]$path) {
  $size = [long]0; $files = 0; $dirs = 0; $mtime = [long]0; $cats = @{}
  $stack = New-Object System.Collections.Stack
  $stack.Push($path)
  while ($stack.Count -gt 0) {
    $d = $stack.Pop()
    try { $items = (New-Object System.IO.DirectoryInfo($d)).GetFileSystemInfos() } catch { continue }
    foreach ($i in $items) {
      try {
        if ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        if ($i.Attributes -band [IO.FileAttributes]::Directory) { $dirs++; $stack.Push($i.FullName); continue }
        $len = [long]$i.Length; $size += $len; $files++
        $t = [long]($i.LastWriteTimeUtc - $epoch).TotalSeconds
        if ($t -gt $mtime -and $t -lt 4000000000) { $mtime = $t }
        $c = $map[$i.Extension.ToLower()]; if (-not $c) { $c = 'other' }
        if (-not $cats.ContainsKey($c)) { $cats[$c] = @([long]0, 0) }
        $cats[$c][0] += $len; $cats[$c][1]++
      } catch { }
    }
  }
  @{ size = $size; files = $files; dirs = $dirs; mtime = $mtime; cats = $cats; trunc = $false }
}

function District([string]$name, [string]$path) {
  Write-Host ("  " + $name)
  $buildings = @(); $looseSize = [long]0; $looseFiles = 0
  try { $items = (New-Object System.IO.DirectoryInfo($path)).GetFileSystemInfos() } catch { $items = @() }
  foreach ($i in ($items | Sort-Object Name)) {
    try {
      if ($i.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
      if ($i.Attributes -band [IO.FileAttributes]::Directory) {
        $b = Walk $i.FullName
        $b.name = $i.Name
        $subs = @()
        try { $subs = @((New-Object System.IO.DirectoryInfo($i.FullName)).GetDirectories() | Select-Object -First 8 | ForEach-Object { $_.Name }) } catch { }
        $b.subs = $subs
        $buildings += , $b
      } else { $looseSize += [long]$i.Length; $looseFiles++ }
    } catch { }
  }
  @{ name = $name; path = $path; buildings = $buildings; loose = @{ size = $looseSize; files = $looseFiles } }
}

Write-Host "Censando el PC. Puede tardar varios minutos..."
$districts = @()
$skip = @('AppData', '.claude', '.docker')
foreach ($d in (Get-ChildItem -LiteralPath $root -Directory -Force | Sort-Object Name)) {
  if ($skip -contains $d.Name) { continue }
  if ($d.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
  $districts += , (District $d.Name $d.FullName)
}
foreach ($s in 'Local', 'Roaming', 'LocalLow') { $districts += , (District ("AppData/" + $s) (Join-Path $root ("AppData\" + $s))) }
foreach ($p in $env:ProgramFiles, ${env:ProgramFiles(x86)}) { if ($p -and (Test-Path $p)) { $districts += , (District (Split-Path $p -Leaf) $p) } }
foreach ($drv in [IO.DriveInfo]::GetDrives()) {
  if ($drv.DriveType -eq 'Fixed' -and $drv.IsReady -and $drv.Name -ne 'C:\') { $districts += , (District ("Disco " + $drv.Name.Substring(0, 2)) $drv.Name) }
}

$c = New-Object IO.DriveInfo('C')
$result = @{
  scanned   = [long]([datetime]::UtcNow - $epoch).TotalSeconds
  root      = $root
  disk      = @{ label = 'C:'; total = [int]($c.TotalSize / 1GB); used = [int](($c.TotalSize - $c.TotalFreeSpace) / 1GB) }
  districts = $districts
}
$json = $result | ConvertTo-Json -Depth 8 -Compress
[IO.File]::WriteAllText($out, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Host ("Listo: " + $out)
