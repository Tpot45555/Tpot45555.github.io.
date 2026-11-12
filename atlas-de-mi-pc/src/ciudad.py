# -*- coding: utf-8 -*-
"""Atlas de mi PC: servidor local.

Censa el disco, sirve la ciudad en http://127.0.0.1:<puerto> y, solo cuando
tu lo confirmas en la pagina, envia carpetas o archivos a la Papelera de
reciclaje de Windows. Solo escucha en este equipo (127.0.0.1).
"""
import os, sys, json, time, threading, secrets, webbrowser, shutil, subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

WIN = os.name == 'nt'
FROZEN = getattr(sys, 'frozen', False)
HERE = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
RES = getattr(sys, '_MEIPASS', HERE)   # donde viven index.html y three.js (dentro del .exe si esta compilado)
LAST_PING = [0.0]
HOME = os.environ.get('CIUDAD_HOME') or os.path.expanduser('~')
TOKEN = secrets.token_urlsafe(24)
SEP = os.sep
PORT = 8791

CAT = {
    'code': 'py js ts tsx jsx html css c cpp h hpp cs java kt rs go rb php sh bat ps1 ipynb lua gd sql vue svelte m scl st',
    'img': 'png jpg jpeg gif bmp webp svg psd tif tiff ico heic raw clip kra xcf ai dds tga',
    'video': 'mp4 mkv avi mov webm wmv flv m4v bik',
    'audio': 'mp3 wav flac ogg m4a aac opus mid wem bnk',
    'doc': 'pdf doc docx xls xlsx ppt pptx txt md odt rtf csv tex epub',
    'zip': 'zip rar 7z tar gz bz2 xz iso jar cab',
    'exe': 'exe dll msi sys bin so apk pak pck vpk',
    'data': 'json xml yaml yml db sqlite dat log ini cfg toml npy pt pth onnx safetensors ckpt h5 parquet pkl gguf',
}
E2C = {e: c for c, l in CAT.items() for e in l.split()}


def cat_of(name):
    i = name.rfind('.')
    return E2C.get(name[i + 1:].lower(), 'other') if i > 0 else 'other'


def norm(p):
    return os.path.normcase(os.path.normpath(p))


def lp(p):
    """Prefijo de ruta larga en Windows para pasar de 260 caracteres."""
    if WIN and not p.startswith('\\\\'):
        return '\\\\?\\' + p
    return p


def key(name):
    return name.lower() if WIN else name


class Node(object):
    __slots__ = ('name', 'parent', 'kids', 'size', 'files', 'dirs', 'mtime', 'cats', 'lsize', 'lfiles', 'old', 'junk', 'jr')

    def __init__(self, name, parent):
        self.name = name; self.parent = parent; self.kids = None
        self.size = 0; self.files = 0; self.dirs = 0; self.mtime = 0
        self.cats = {}; self.lsize = 0; self.lfiles = 0
        self.old = 0; self.junk = 0; self.jr = None


LOCK = threading.RLock()
ROOTS = {}      # norm(path) -> (Node, path)
PLAN = []       # [{'label','path' or None,'buildings':[path,...]}]
STATE = {'done': False, 'files': 0, 'current': '', 'scanned': 0, 'running': False}

LINK_TAGS = (0xA000000C, 0xA0000003)  # enlace simbolico, union
YEAR = 365 * 86400
OLD_CUT = [time.time() - YEAR]      # lo que no cambia desde antes de esta fecha cuenta como abandonado

# ---- basura conocida: carpetas que casi siempre se regeneran solas
J_ANY = {'node_modules': 'Dependencias de Node: se reinstalan con npm install',
         '__pycache__': 'Cach\u00e9 de Python: se vuelve a crear sola'}
J_CACHE = ('cache', 'caches', 'code cache', 'gpucache', 'shadercache', 'dxcache', 'glcache', 'cachestorage', 'cache_data',
           'd3dscache', 'inetcache', 'npm-cache', '.cache', 'grshadercache', 'dawncache', 'cacheddata', 'shader_cache')
J_TEMP = ('temp', 'tmp')
J_CRASH = ('crashdumps', 'crashpad', 'crash reports', 'crashes')
J_NAMES = set(J_ANY) | set(J_CACHE) | set(J_TEMP) | set(J_CRASH)
_HOMEK = [None, None]


def junk_reason(name, parent_path):
    n = name.lower()
    if n not in J_NAMES:
        return None
    if n in J_ANY:
        return J_ANY[n]
    if _HOMEK[0] is None:
        _HOMEK[0] = norm(HOME) + SEP
        _HOMEK[1] = norm(os.path.join(HOME, 'AppData')) + SEP
    k = norm(parent_path) + SEP
    if n == 'caches' and os.path.basename(parent_path).lower() == '.gradle':
        return 'Cach\u00e9 de Gradle: se vuelve a descargar sola'
    inapp = k.startswith(_HOMEK[1]) or (k.startswith(_HOMEK[0]) and k[len(_HOMEK[0]):].startswith('.')) \
        or (k == _HOMEK[0] and n == '.cache')
    if not inapp:
        return None
    if n in J_TEMP:
        return 'Archivos temporales'
    if n in J_CRASH:
        return 'Volcados de errores de programas'
    return 'Cach\u00e9 de aplicaci\u00f3n: se vuelve a crear sola'


def is_link(entry, st):
    if entry.is_symlink():
        return True
    return getattr(st, 'st_reparse_tag', 0) in LINK_TAGS


def scan_tree(path, name):
    root = Node(name, None)
    stack = [(root, path)]
    order = [root]
    cut = OLD_CUT[0]
    while stack:
        node, p = stack.pop()
        try:
            it = os.scandir(lp(p))
        except OSError:
            continue
        n = 0
        with it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    if is_link(e, st):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        ch = Node(e.name, node)
                        ch.jr = junk_reason(e.name, p)
                        if node.kids is None:
                            node.kids = {}
                        node.kids[key(e.name)] = ch
                        order.append(ch)
                        stack.append((ch, os.path.join(p, e.name)))
                    else:
                        s = st.st_size
                        node.size += s; node.lsize += s
                        node.files += 1; node.lfiles += 1
                        m = st.st_mtime
                        if m < cut:
                            node.old += s
                        if node.mtime < m < 4e9:
                            node.mtime = m
                        c = node.cats.get(cat_of(e.name))
                        if c is None:
                            node.cats[cat_of(e.name)] = [s, 1]
                        else:
                            c[0] += s; c[1] += 1
                        n += 1
                except OSError:
                    pass
        STATE['files'] += n
    for nd in reversed(order):
        p = nd.parent
        if nd.jr:
            nd.junk = nd.size
        if p is None:
            continue
        p.old += nd.old; p.junk += nd.junk
        p.size += nd.size; p.files += nd.files; p.dirs += nd.dirs + 1
        if nd.mtime > p.mtime:
            p.mtime = nd.mtime
        for c, a in nd.cats.items():
            b = p.cats.get(c)
            if b is None:
                p.cats[c] = [a[0], a[1]]
            else:
                b[0] += a[0]; b[1] += a[1]
    return root


def subdirs(path):
    out = []
    try:
        with os.scandir(lp(path)) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    if not is_link(e, st) and e.is_dir(follow_symlinks=False):
                        out.append(e.name)
                except OSError:
                    pass
    except OSError:
        pass
    return sorted(out, key=str.lower)


def fixed_drives():
    if not WIN:
        return []
    import ctypes
    out = []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    for i in range(26):
        if mask & (1 << i):
            d = chr(65 + i) + ':\\'
            if ctypes.windll.kernel32.GetDriveTypeW(d) == 3:
                out.append(d)
    return out


def sysdrive():
    return (os.environ.get('SystemDrive', 'C:') + '\\') if WIN else None


def make_plan():
    plan = []
    for n in subdirs(HOME):
        if n == 'AppData':
            continue
        plan.append({'label': n, 'path': os.path.join(HOME, n), 'buildings': None})
    for s in ('Local', 'Roaming', 'LocalLow'):
        p = os.path.join(HOME, 'AppData', s)
        if os.path.isdir(p):
            plan.append({'label': 'AppData \u00b7 ' + s, 'path': p, 'buildings': None})
    if WIN:
        sd = sysdrive()
        named = []
        for v in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramData'):
            p = os.environ.get(v)
            if p and os.path.isdir(p) and norm(p) not in [norm(x) for x in named]:
                named.append(p)
                plan.append({'label': os.path.basename(p), 'path': p, 'buildings': None})
        users = os.path.dirname(HOME)
        skip = set(norm(x) for x in named + [users])
        skipn = ('$recycle.bin', 'system volume information', 'recovery', 'config.msi', '$windows.~bt', '$windows.~ws')
        rest = [os.path.join(sd, n) for n in subdirs(sd) if n.lower() not in skipn and norm(os.path.join(sd, n)) not in skip]
        if rest:
            plan.append({'label': 'Disco ' + sd[:2] + ' (resto)', 'path': None, 'buildings': rest})
        others = [os.path.join(users, n) for n in subdirs(users) if norm(os.path.join(users, n)) != norm(HOME)]
        if others:
            plan.append({'label': 'Otros usuarios', 'path': None, 'buildings': others})
        for d in fixed_drives():
            if norm(d) != norm(sd):
                plan.append({'label': 'Disco ' + d[:2], 'path': d, 'buildings': None})
    return plan


def do_scan():
    STATE.update(done=False, files=0, current='', running=True)
    OLD_CUT[0] = time.time() - YEAR
    plan = make_plan()
    roots = {}
    for d in plan:
        targets = [d['path']] if d['path'] else d['buildings']
        for p in targets:
            STATE['current'] = p
            name = os.path.basename(p.rstrip(SEP)) or p
            roots[norm(p)] = (scan_tree(p, name), p)
    with LOCK:
        ROOTS.clear(); ROOTS.update(roots)
        PLAN[:] = plan
    STATE.update(done=True, running=False, scanned=int(time.time()), current='')


def find(path):
    """Devuelve (nodo, ruta_raiz, [nombres desde la raiz]) o None."""
    k = norm(path)
    best = None
    for rk, (node, rp) in ROOTS.items():
        pre = rk if rk.endswith(SEP) else rk + SEP
        if k == rk or k.startswith(pre):
            if best is None or len(rk) > len(best[0]):
                best = (rk, node, rp)
    if best is None:
        return None
    rk, node, rp = best
    rel = k[len(rk):].strip(SEP)
    names = []
    if rel:
        for part in rel.split(SEP):
            if not node.kids or part not in node.kids:
                return None
            node = node.kids[part]
            names.append(node.name)
    return node, rp, names


# ---------------------------------------------------------------- proteccion
SYSFILES = ('pagefile.sys', 'hiberfil.sys', 'swapfile.sys', 'dumpstack.log.tmp', 'ntuser.dat', 'bootmgr')


def protected(path):
    """Motivo por el que NO se puede borrar, o None si se puede."""
    k = norm(path)
    if os.path.dirname(k) == k:
        return 'Es la ra\u00edz de un disco.'
    exact = [HOME, os.path.dirname(HOME), os.path.join(HOME, 'AppData')]
    with LOCK:
        exact += [d['path'] for d in PLAN if d['path']]
    for p in exact:
        if k == norm(p):
            return 'Es una carpeta base del sistema o un vecindario completo. Entra y borra lo de adentro.'
    if os.path.dirname(k) == norm(os.path.dirname(HOME)):
        return 'Es el perfil de otro usuario del equipo.'
    here = norm(HERE)
    if here == k or here.startswith(k + SEP):
        return 'Contiene esta misma aplicaci\u00f3n.'
    if WIN:
        win = norm(os.environ.get('SystemRoot', 'C:\\Windows'))
        if k == win or k.startswith(win + SEP):
            return 'Pertenece a Windows.'
        if os.path.basename(k) in SYSFILES or os.path.basename(k).startswith('ntuser.'):
            return 'Es un archivo del sistema.'
    return None


def risky(path):
    k = norm(path) + SEP
    zones = [os.path.join(HOME, 'AppData')]
    if WIN:
        zones += [os.environ.get(v) for v in ('ProgramFiles', 'ProgramFiles(x86)', 'ProgramData') if os.environ.get(v)]
    for z in zones:
        if k.startswith(norm(z) + SEP):
            return True
    return False


# ---------------------------------------------------------------- papelera
def recycle(path):
    """Envia a la Papelera. Devuelve None si salio bien o un texto de error."""
    if not WIN:  # solo para pruebas fuera de Windows
        dst = os.path.join(HERE, '_papelera_prueba')
        os.makedirs(dst, exist_ok=True)
        shutil.move(path, os.path.join(dst, '%d_%s' % (time.time(), os.path.basename(path))))
        return None
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT), ('pFrom', ctypes.c_void_p),
                    ('pTo', ctypes.c_void_p), ('fFlags', ctypes.c_ushort), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', ctypes.c_void_p)]

    buf = ctypes.create_unicode_buffer(path, len(path) + 2)  # termina en doble nulo
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3                                  # FO_DELETE
    op.pFrom = ctypes.addressof(buf)
    # ALLOWUNDO (papelera) + NOCONFIRMATION + WANTNUKEWARNING:
    # si algo no cabe en la papelera, Windows pregunta antes de borrarlo del todo.
    op.fFlags = 0x0040 | 0x0010 | 0x4000
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if rc != 0:
        return 'Windows no pudo enviarlo a la papelera (c\u00f3digo %d). Puede estar en uso o tener una ruta demasiado larga.' % rc
    if op.fAnyOperationsAborted:
        return 'Operaci\u00f3n cancelada.'
    return None


def bin_info():
    if not WIN:
        return None
    try:
        import ctypes

        class Q(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_ulong), ('i64Size', ctypes.c_longlong), ('i64NumItems', ctypes.c_longlong)]
        q = Q(); q.cbSize = ctypes.sizeof(Q)
        if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(q)) == 0:
            return {'size': q.i64Size, 'items': q.i64NumItems}
    except Exception:
        pass
    return None


def disk_info():
    root = sysdrive() or HOME
    try:
        u = shutil.disk_usage(root)
        d = {'label': root[:2] if WIN else root, 'total': u.total, 'used': u.used, 'free': u.free}
    except OSError:
        d = {'label': '?', 'total': 1, 'used': 0, 'free': 0}
    d['bin'] = bin_info()
    return d


# ---------------------------------------------------------------- vistas
def entry(node, path):
    return {'name': node.name, 'path': path, 'kind': 'dir', 'size': node.size, 'files': node.files, 'dirs': node.dirs,
            'mtime': int(node.mtime), 'cats': node.cats, 'prot': protected(path), 'risky': risky(path),
            'old': node.old, 'junk': node.junk, 'jr': node.jr}


def city():
    out = []
    with LOCK:
        for d in PLAN:
            if d['path']:
                r = ROOTS.get(norm(d['path']))
                if not r:
                    continue
                node = r[0]
                bs = [entry(ch, os.path.join(d['path'], ch.name)) for ch in (node.kids or {}).values()]
                out.append({'label': d['label'], 'path': d['path'], 'buildings': bs,
                            'loose': {'size': node.lsize, 'files': node.lfiles}})
            else:
                bs = []
                for p in d['buildings']:
                    r = ROOTS.get(norm(p))
                    if r:
                        bs.append(entry(r[0], p))
                out.append({'label': d['label'], 'path': None, 'buildings': bs, 'loose': {'size': 0, 'files': 0}})
    return {'root': HOME, 'scanned': STATE['scanned'], 'disk': disk_info(), 'districts': out, 'drives': drives_info(),
            'jarvis': os.path.isdir(JARVIS_RUN)}


TOP = 220


def node_view(path):
    with LOCK:
        f = find(path)
        if not f:
            return None
        node, rp, names = f
        real = os.path.join(rp, *names) if names else rp
        crumbs = [{'name': os.path.basename(rp.rstrip(SEP)) or rp, 'path': rp}]
        acc = rp
        for n in names:
            acc = os.path.join(acc, n)
            crumbs.append({'name': n, 'path': acc})
        dirs = sorted((node.kids or {}).values(), key=lambda c: -c.size)
        items = [entry(c, os.path.join(real, c.name)) for c in dirs[:TOP]]
        if len(dirs) > TOP:
            rest = dirs[TOP:]
            items.append({'name': '%d carpetas peque\u00f1as' % len(rest), 'kind': 'rest', 'path': None,
                          'size': sum(c.size for c in rest), 'files': sum(c.files for c in rest), 'dirs': len(rest),
                          'mtime': 0, 'cats': {}, 'prot': 'Es un grupo de carpetas peque\u00f1as.', 'risky': False,
                          'old': sum(c.old for c in rest), 'junk': sum(c.junk for c in rest), 'jr': None})
        is_district = not names and any(d['path'] and norm(d['path']) == norm(rp) for d in PLAN)
        res = {'path': real, 'name': crumbs[-1]['name'], 'size': node.size, 'files': node.files, 'dirs': node.dirs,
               'crumbs': crumbs, 'parent': os.path.dirname(real) if names else None, 'district': is_district}
        node_junk = 1 if (node.size and node.junk >= node.size) else 0
    files = []
    try:
        with os.scandir(lp(real)) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    if is_link(e, st) or e.is_dir(follow_symlinks=False):
                        continue
                    files.append((st.st_size, e.name, int(st.st_mtime)))
                except OSError:
                    pass
    except OSError:
        pass
    files.sort(reverse=True)
    for s, n, m in files[:TOP]:
        p = os.path.join(real, n)
        c = cat_of(n)
        items.append({'name': n, 'kind': 'file', 'path': p, 'size': s, 'files': 1, 'dirs': 0, 'mtime': m,
                      'cats': {c: [s, 1]}, 'prot': protected(p), 'risky': risky(p),
                      'old': s if m < OLD_CUT[0] else 0, 'junk': node_junk * s, 'jr': None})
    if len(files) > TOP:
        rest = files[TOP:]
        items.append({'name': '%d archivos peque\u00f1os' % len(rest), 'kind': 'rest', 'path': None,
                      'size': sum(x[0] for x in rest), 'files': len(rest), 'dirs': 0, 'mtime': 0, 'cats': {},
                      'prot': 'Es un grupo de archivos peque\u00f1os.', 'risky': False,
                      'old': sum(x[0] for x in rest if x[2] < OLD_CUT[0]), 'junk': node_junk * sum(x[0] for x in rest), 'jr': None})
    res['items'] = items
    return res


def fix_junk(a):
    a.junk = a.size if a.jr else sum(k.junk for k in (a.kids or {}).values())


def forget(path, fsize, was_dir, was_old=False):
    """Quita del censo en memoria lo que se acaba de borrar."""
    with LOCK:
        if was_dir:
            f = find(path)
            if not f:
                return
            node = f[0]
            a = node.parent
            if a is None:
                ROOTS.pop(norm(path), None)
                for d in PLAN:
                    if d['buildings']:
                        d['buildings'] = [b for b in d['buildings'] if norm(b) != norm(path)]
                return
            a.kids.pop(key(node.name), None)
            while a is not None:
                a.size -= node.size; a.files -= node.files; a.dirs -= node.dirs + 1
                a.old = max(0, a.old - node.old)
                for c, v in node.cats.items():
                    if c in a.cats:
                        a.cats[c][0] -= v[0]; a.cats[c][1] -= v[1]
                        if a.cats[c][1] <= 0:
                            del a.cats[c]
                fix_junk(a)
                a = a.parent
        else:
            f = find(os.path.dirname(path))
            if not f:
                return
            a = f[0]
            c = cat_of(os.path.basename(path))
            a.lsize -= fsize; a.lfiles -= 1
            while a is not None:
                a.size -= fsize; a.files -= 1
                if was_old:
                    a.old = max(0, a.old - fsize)
                if c in a.cats:
                    a.cats[c][0] -= fsize; a.cats[c][1] -= 1
                    if a.cats[c][1] <= 0:
                        del a.cats[c]
                fix_junk(a)
                a = a.parent


def delete(path):
    path = os.path.normpath(path)
    if not os.path.isabs(path):
        return {'ok': False, 'error': 'Ruta no v\u00e1lida.'}
    reason = protected(path)
    if reason:
        return {'ok': False, 'error': reason}
    with LOCK:
        inside = find(path) or find(os.path.dirname(path))
    if not inside:
        return {'ok': False, 'error': 'Esa ruta no est\u00e1 en el censo.'}
    if not os.path.lexists(lp(path)):
        return {'ok': False, 'error': 'Ya no existe en el disco. Vuelve a censar.'}
    was_dir = os.path.isdir(lp(path))
    with LOCK:
        f = find(path) if was_dir else None
    was_old = False
    try:
        if f:
            size = f[0].size
        else:
            st = os.stat(lp(path)); size = st.st_size; was_old = st.st_mtime < OLD_CUT[0]
    except OSError:
        size = 0
    try:
        err = recycle(path)
    except Exception as ex:
        err = 'No se pudo enviar a la papelera: %s' % ex
    if err:
        return {'ok': False, 'error': err}
    if os.path.lexists(lp(path)):
        return {'ok': False, 'error': 'Sigue en el disco: se cancel\u00f3 o est\u00e1 en uso.'}
    forget(path, size, was_dir, was_old)
    return {'ok': True, 'freed': size, 'disk': disk_info()}


def open_in_explorer(path):
    if not os.path.lexists(path):
        return {'ok': False, 'error': 'Ya no existe.'}
    if WIN:
        if os.path.isdir(path):
            os.startfile(path)
        else:
            subprocess.Popen('explorer /select,"%s"' % path)
    return {'ok': True}


def empty_bin():
    if WIN:
        import ctypes
        ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0)  # Windows pide su propia confirmacion
    return {'ok': True, 'disk': disk_info()}


# ---------------------------------------------------------------- informes
MINREP = 50 * 1024 * 1024


def system_zone(path):
    if not WIN:
        return False
    k = norm(path) + SEP
    for v in ('SystemRoot', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData'):
        z = os.environ.get(v)
        if z and k.startswith(norm(z) + SEP):
            return True
    return False


def report(kind):
    """Lista los bloques mas grandes que estan abandonados enteros ('old') o son basura conocida ('junk')."""
    out = []
    loose_dirs = []
    with LOCK:
        stack = [(n, p) for n, p in ROOTS.values() if kind == 'junk' or not system_zone(p)]
        while stack:
            node, path = stack.pop()
            if kind == 'junk':
                if node.jr:
                    if node.size >= 5 * 1024 * 1024:
                        out.append({'name': node.name, 'path': path, 'kind': 'dir', 'size': node.size, 'files': node.files,
                                    'mtime': int(node.mtime), 'why': node.jr})
                    continue
                if node.junk < 5 * 1024 * 1024:
                    continue
            else:
                if node.old < MINREP:
                    continue
                if node.size and node.old >= node.size * 0.98 and node.parent is not None and not protected(path):
                    out.append({'name': node.name, 'path': path, 'kind': 'dir', 'size': node.size, 'files': node.files,
                                'mtime': int(node.mtime), 'why': 'Nada cambia aquí desde ' + time.strftime('%Y', time.localtime(node.mtime))})
                    continue
                if node.lsize >= MINREP:
                    loose_dirs.append(path)
            for ch in (node.kids or {}).values():
                stack.append((ch, os.path.join(path, ch.name)))
    for d in loose_dirs[:400]:      # archivos sueltos grandes y viejos
        try:
            with os.scandir(lp(d)) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        if e.is_dir(follow_symlinks=False) or is_link(e, st):
                            continue
                        if st.st_size >= MINREP and st.st_mtime < OLD_CUT[0]:
                            p = os.path.join(d, e.name)
                            if not protected(p):
                                out.append({'name': e.name, 'path': p, 'kind': 'file', 'size': st.st_size, 'files': 1,
                                            'mtime': int(st.st_mtime), 'why': 'Archivo sin cambios desde ' + time.strftime('%Y', time.localtime(st.st_mtime))})
                    except OSError:
                        pass
        except OSError:
            pass
    if kind == 'junk':               # instaladores viejos en Descargas
        dl = os.path.join(HOME, 'Downloads')
        try:
            with os.scandir(lp(dl)) as it:
                for e in it:
                    try:
                        st = e.stat(follow_symlinks=False)
                        if e.is_file(follow_symlinks=False) and e.name.lower().endswith(('.exe', '.msi', '.iso')) \
                                and st.st_mtime < time.time() - 90 * 86400 and st.st_size >= 5 * 1024 * 1024:
                            out.append({'name': e.name, 'path': os.path.join(dl, e.name), 'kind': 'file', 'size': st.st_size,
                                        'files': 1, 'mtime': int(st.st_mtime), 'why': 'Instalador en Descargas con más de 90 días'})
                    except OSError:
                        pass
        except OSError:
            pass
    out.sort(key=lambda x: -x['size'])
    return {'kind': kind, 'total': sum(x['size'] for x in out), 'count': len(out), 'items': out[:300]}


# ---------------------------------------------------------------- gemelos (duplicados)
DUP = {'state': 'idle', 'phase': '', 'files': 0, 'bytes': 0, 'total': 0, 'groups': [], 'wasted': 0, 'min': 0}
CLOUD = 0x400000 | 0x1000      # archivos solo en la nube: no se leen para no descargarlos


def sig(path, size):
    import hashlib
    h = hashlib.blake2b(digest_size=16)
    with open(lp(path), 'rb') as f:
        if size <= 64 * 1024 * 1024:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h.update(b); DUP['bytes'] += len(b)
        else:                    # archivos grandes: tres muestras de 4 MB (inicio, centro, final)
            for off in (0, size // 2, max(0, size - (4 << 20))):
                f.seek(off); b = f.read(4 << 20); h.update(b); DUP['bytes'] += len(b)
    return h.hexdigest()


def find_dupes(minsize):
    DUP.update(state='running', phase='Listando archivos', files=0, bytes=0, total=0, groups=[], wasted=0, min=minsize)
    try:
        with LOCK:
            roots = [d['path'] for d in PLAN if d['path'] and not system_zone(d['path'])
                     and not (norm(d['path']) + SEP).startswith(norm(os.path.join(HOME, 'AppData')) + SEP)]
        by = {}
        for r in roots:
            stack = [r]
            while stack:
                d = stack.pop()
                try:
                    it = os.scandir(lp(d))
                except OSError:
                    continue
                with it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                            if is_link(e, st):
                                continue
                            if e.is_dir(follow_symlinks=False):
                                if not junk_reason(e.name, d):
                                    stack.append(os.path.join(d, e.name))
                            elif st.st_size >= minsize and not (getattr(st, 'st_file_attributes', 0) & CLOUD):
                                by.setdefault(st.st_size, []).append((os.path.join(d, e.name), int(st.st_mtime)))
                                DUP['files'] += 1
                        except OSError:
                            pass
        cand = [(s, fs) for s, fs in by.items() if len(fs) > 1]
        DUP['phase'] = 'Comparando contenido'
        DUP['total'] = sum(min(s, 12 << 20) * len(fs) for s, fs in cand)
        groups = []
        for s, fs in sorted(cand, key=lambda x: -x[0]):
            hs = {}
            for p, m in fs:
                try:
                    hs.setdefault(sig(p, s), []).append({'path': p, 'mtime': m})
                except OSError:
                    pass
            for g in hs.values():
                if len(g) > 1:
                    g.sort(key=lambda x: x['mtime'])
                    groups.append({'size': s, 'sampled': s > 64 * 1024 * 1024, 'files': g})
        groups.sort(key=lambda g: -g['size'] * (len(g['files']) - 1))
        DUP.update(groups=groups[:400], wasted=sum(g['size'] * (len(g['files']) - 1) for g in groups), state='done', phase='')
    except Exception as ex:
        DUP.update(state='error', phase=str(ex))


# ---------------------------------------------------------------- mudanza a otro disco
MOVE = {'active': False, 'bytes': 0, 'total': 0}


def drive_of(p):
    if WIN:
        return os.path.splitdrive(os.path.abspath(p))[0].upper() + '\\'
    best = ''
    for d in test_drives():
        if (norm(p) + SEP).startswith(norm(d) + SEP) and len(d) > len(best):
            best = d
    return best or '/'


def test_drives():
    v = os.environ.get('CIUDAD_DRIVES')
    return v.split(os.pathsep) if v else []


def drives_info():
    out = []
    for d in (fixed_drives() if WIN else test_drives()):
        try:
            u = shutil.disk_usage(d)
            out.append({'root': d, 'free': u.free, 'total': u.total})
        except OSError:
            pass
    return out


def tree_bytes(path):
    if os.path.isfile(lp(path)):
        return os.path.getsize(lp(path)), 1
    tot = n = 0
    for base, _, files in os.walk(lp(path)):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(base, f)); n += 1
            except OSError:
                pass
    return tot, n


def move(path, dest, link):
    path = os.path.normpath(path); dest = os.path.normpath(dest)
    if MOVE['active']:
        return {'ok': False, 'error': 'Ya hay una mudanza en curso.'}
    if not os.path.isabs(path) or not os.path.isabs(dest):
        return {'ok': False, 'error': 'Ruta no válida.'}
    reason = protected(path)
    if reason:
        return {'ok': False, 'error': reason}
    with LOCK:
        if not (find(path) or find(os.path.dirname(path))):
            return {'ok': False, 'error': 'Esa ruta no está en el censo.'}
    if not os.path.lexists(lp(path)):
        return {'ok': False, 'error': 'Ya no existe en el disco.'}
    roots = [norm(d['root']) for d in drives_info()]
    if norm(drive_of(dest)) not in roots or norm(drive_of(dest)) == norm(drive_of(path)):
        return {'ok': False, 'error': 'El destino tiene que estar en otro disco fijo.'}
    if os.path.lexists(lp(dest)):
        return {'ok': False, 'error': 'Ya existe algo con ese nombre en el destino. Cambia la carpeta de destino.'}
    if protected(dest) or system_zone(dest):
        return {'ok': False, 'error': 'Ese destino es una zona del sistema.'}
    was_dir = os.path.isdir(lp(path))
    size, count = tree_bytes(path)
    if shutil.disk_usage(drive_of(dest)).free < size * 1.02 + (64 << 20):
        return {'ok': False, 'error': 'No hay espacio suficiente en el disco de destino.'}
    was_old = (not was_dir) and os.stat(lp(path)).st_mtime < OLD_CUT[0]
    MOVE.update(active=True, bytes=0, total=size)

    def cp(s, d):
        shutil.copy2(s, d)
        try:
            MOVE['bytes'] += os.path.getsize(d)
        except OSError:
            pass
    try:
        os.makedirs(lp(os.path.dirname(dest)), exist_ok=True)
        if was_dir:
            shutil.copytree(lp(path), lp(dest), copy_function=cp, symlinks=True)
        else:
            cp(lp(path), lp(dest))
        got, n = tree_bytes(dest)
        if got != size or n != count:
            raise IOError('la copia no coincide con el original (%d de %d archivos)' % (n, count))
    except Exception as ex:           # la copia fallo: el original queda intacto y se limpia lo copiado a medias
        try:
            if os.path.isdir(lp(dest)):
                shutil.rmtree(lp(dest), ignore_errors=True)
            elif os.path.lexists(lp(dest)):
                os.remove(lp(dest))
        except OSError:
            pass
        MOVE['active'] = False
        return {'ok': False, 'error': 'No se pudo copiar, el original sigue intacto: %s' % ex}
    warn = None

    def retry(fn, p, _):
        try:
            os.chmod(p, 0o700); fn(p)
        except OSError:
            pass
    try:                              # copia verificada: ahora si se quita el original
        if was_dir:
            shutil.rmtree(lp(path), onerror=retry)
        else:
            try:
                os.remove(lp(path))
            except OSError:
                os.chmod(lp(path), 0o700); os.remove(lp(path))
    except OSError:
        pass
    if os.path.lexists(lp(path)):
        warn = 'La copia está completa en el destino, pero parte del original no se pudo quitar (archivos en uso). Quedó en ambos sitios.'
    else:
        forget(path, size, was_dir, was_old)
        if link and was_dir and WIN:
            r = subprocess.run(['cmd', '/c', 'mklink', '/J', path, dest], capture_output=True)
            if r.returncode != 0:
                warn = 'Se movió, pero no se pudo crear el enlace en el lugar original.'
    MOVE['active'] = False
    return {'ok': True, 'moved': size, 'dest': dest, 'warn': warn, 'disk': disk_info()}


# ---------------------------------------------------------------- jarvis lo narra
JARVIS_RUN = os.path.join(HOME, 'Documents', 'Jarvis', 'run')


def spoken(b):
    if b >= 1 << 30:
        v = b / float(1 << 30); u = 'gigas'
    elif b >= 1 << 20:
        v = b / float(1 << 20); u = 'megas'
    else:
        return 'menos de un mega'
    return (('%.0f' % v) if v >= 20 else ('%.1f' % v).replace('.', ',').replace(',0', '')) + ' ' + u


def say(q, path):
    with LOCK:
        if q == 'top':
            f = find(path) if path else None
            if f:
                node = f[0]; where = 'en ' + node.name
                kids = sorted((node.kids or {}).values(), key=lambda c: -c.size)[:3]
                names = [(k.name, k.size) for k in kids]
            else:
                where = 'en todo el equipo'
                allb = []
                for d in PLAN:
                    if d['path'] and norm(d['path']) in ROOTS:
                        n = ROOTS[norm(d['path'])][0]
                        allb.append((d['label'].replace('·', ''), n.size))
                    elif d['buildings']:
                        for b in d['buildings']:
                            if norm(b) in ROOTS:
                                allb.append((ROOTS[norm(b)][0].name, ROOTS[norm(b)][0].size))
                names = sorted(allb, key=lambda x: -x[1])[:3]
            if not names:
                text = 'No encontré carpetas %s.' % where
            else:
                text = 'Lo que más pesa %s: ' % where + ', '.join('%s con %s' % (n, spoken(s)) for n, s in names) + '.'
        elif q == 'disk':
            d = disk_info()
            text = 'El disco %s tiene %s libres de %s.' % (d['label'].replace(':', ''), spoken(d['free']), spoken(d['total']))
            if d.get('bin') and d['bin']['size']:
                text += ' La papelera guarda %s que se liberan al vaciarla.' % spoken(d['bin']['size'])
        else:
            text = None
    if text is None:
        j = report('junk'); o = report('old')
        text = 'Hay %s de basura conocida, que se regenera sola' % spoken(j['total'])
        if j['items']:
            text += '; lo más grande es %s con %s' % (j['items'][0]['name'], spoken(j['items'][0]['size']))
        text += '. Además hay %s que nadie toca hace más de un año' % spoken(o['total'])
        if o['items']:
            text += '; lo más grande es %s con %s' % (o['items'][0]['name'], spoken(o['items'][0]['size']))
        text += '. Revise los informes antes de borrar.'
    done = False
    try:
        if os.path.isdir(JARVIS_RUN):
            with open(os.path.join(JARVIS_RUN, 'decir.txt'), 'w', encoding='utf-8') as f:
                f.write(text)
            done = True
    except OSError:
        pass
    return {'text': text, 'spoken': done}


# ---------------------------------------------------------------- http
STATIC = {'/three.min.js': 'application/javascript', '/OrbitControls.js': 'application/javascript'}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def allowed(self):
        host = self.headers.get('Host', '')
        if host not in ('127.0.0.1:%d' % PORT, 'localhost:%d' % PORT):
            return False
        return secrets.compare_digest(self.headers.get('X-Token', ''), TOKEN)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            if self.headers.get('Host', '') not in ('127.0.0.1:%d' % PORT, 'localhost:%d' % PORT):
                return self.send(403, {'error': 'host'})
            html = open(os.path.join(RES, 'index.html'), encoding='utf-8').read().replace('__TOKEN__', TOKEN)
            return self.send(200, html.encode('utf-8'), 'text/html; charset=utf-8')
        if u.path in STATIC:
            return self.send(200, open(os.path.join(RES, u.path[1:]), 'rb').read(), STATIC[u.path])
        if not u.path.startswith('/api/'):
            return self.send(404, {'error': 'no existe'})
        if not self.allowed():
            return self.send(403, {'error': 'No autorizado. Recarga la p\u00e1gina.'})
        LAST_PING[0] = time.time()
        if u.path == '/api/ping':
            return self.send(200, {'ok': True})
        if u.path == '/api/status':
            return self.send(200, {k: STATE[k] for k in ('done', 'files', 'current', 'running')})
        if not STATE['done']:
            return self.send(409, {'error': 'El censo sigue en curso.'})
        if u.path == '/api/city':
            return self.send(200, city())
        if u.path == '/api/report':
            return self.send(200, report('junk' if parse_qs(u.query).get('kind', [''])[0] == 'junk' else 'old'))
        if u.path == '/api/dupes':
            return self.send(200, DUP)
        if u.path == '/api/progress':
            return self.send(200, MOVE)
        if u.path == '/api/node':
            v = node_view(parse_qs(u.query).get('path', [''])[0])
            return self.send(200, v) if v else self.send(404, {'error': 'Esa carpeta ya no est\u00e1 en el censo.'})
        self.send(404, {'error': 'no existe'})

    def do_POST(self):
        u = urlparse(self.path)
        if not self.allowed():
            return self.send(403, {'error': 'No autorizado. Recarga la p\u00e1gina.'})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0)) or 0) or b'{}')
        except ValueError:
            return self.send(400, {'error': 'Petici\u00f3n no v\u00e1lida.'})
        if u.path == '/api/quit':
            threading.Timer(0.4, lambda: os._exit(0)).start()
            return self.send(200, {'ok': True})
        if u.path == '/api/rescan':
            if not STATE['running']:
                STATE['done'] = False
                threading.Thread(target=do_scan, daemon=True).start()
            return self.send(200, {'ok': True})
        if not STATE['done']:
            return self.send(409, {'error': 'El censo sigue en curso.'})
        if u.path == '/api/delete':
            return self.send(200, delete(str(body.get('path', ''))))
        if u.path == '/api/move':
            return self.send(200, move(str(body.get('path', '')), str(body.get('dest', '')), bool(body.get('link'))))
        if u.path == '/api/dupes':
            if DUP['state'] != 'running':
                DUP['state'] = 'running'
                mb = max(1, min(4096, int(body.get('min', 10) or 10)))
                threading.Thread(target=find_dupes, args=(mb << 20,), daemon=True).start()
            return self.send(200, {'ok': True})
        if u.path == '/api/say':
            return self.send(200, say(str(body.get('q', '')), body.get('path') or None))
        if u.path == '/api/open':
            return self.send(200, open_in_explorer(str(body.get('path', ''))))
        if u.path == '/api/emptybin':
            return self.send(200, empty_bin())
        self.send(404, {'error': 'no existe'})


def open_window(url):
    """Abre la ciudad en una ventana propia (Edge en modo app); si no hay Edge, en el navegador."""
    if WIN:
        for base in (os.environ.get('ProgramFiles(x86)'), os.environ.get('ProgramFiles'), os.environ.get('LocalAppData')):
            exe = os.path.join(base or '', 'Microsoft', 'Edge', 'Application', 'msedge.exe')
            if base and os.path.isfile(exe):
                try:
                    subprocess.Popen([exe, '--app=' + url, '--window-size=1500,920'])
                    return
                except OSError:
                    pass
    webbrowser.open(url)


def watchdog():
    """El .exe no tiene consola: se apaga solo cuando la ventana lleva un rato cerrada."""
    start = time.time()
    while True:
        time.sleep(10)
        last = LAST_PING[0]
        if (last and time.time() - last > 150) or (not last and time.time() - start > 300):
            os._exit(0)


def main():
    global PORT
    srv = None
    for p in range(8791, 8811):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', p), H); PORT = p; break
        except OSError:
            continue
    if srv is None:
        print('No hay puerto libre entre 8791 y 8810.'); return 1
    threading.Thread(target=do_scan, daemon=True).start()
    url = 'http://127.0.0.1:%d/' % PORT
    print('Atlas de mi PC en %s' % url)
    print('Deja esta ventana abierta mientras lo usas. Cierrala para apagarlo.')
    if not os.environ.get('CIUDAD_NO_BROWSER'):
        threading.Timer(0.6, lambda: open_window(url)).start()
    if FROZEN:
        threading.Thread(target=watchdog, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
