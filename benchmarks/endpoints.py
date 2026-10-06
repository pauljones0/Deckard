#!/usr/bin/env python3
"""Native endpoint throughput comparison. Timing excludes the later IPC frame observer."""
import argparse, hashlib, json, os, pathlib, platform, statistics, subprocess, time
from types import SimpleNamespace
from accounting import CpuAccounting
import psutil
from PIL import Image, ImageDraw
from compare import native_status, memory_snapshot, stop, start_compositor

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODELS = {"mini": (2, 3, (80,80)), "mk2": (3,5,(72,72)), "xl": (4,8,(96,96)), "plus": (2,4,(120,120)), "plus-xl": (4,9,(112,112)), "studio": (2,16,(144,112)), "ulanzi": (3,5,(196,196)), "mirabox": (3,6,(85,85))}
CASES = {"plus10":("plus",10,"gif"), "plus50":("plus",50,"gif"), "plus100":("plus",100,"gif"), "plus60video":("plus",60,"video"), "plus60once":("plus",60,"video-once"), "xl50":("xl",50,"gif"), "plusxl50":("plus-xl",50,"gif"), "mini100":("mini",100,"gif"), "mk2100":("mk2",100,"gif"), "studio50":("studio",50,"gif"), "ulanzi50":("ulanzi",50,"gif"), "mirabox100":("mirabox",100,"gif")}

def asset(directory, rate, kind):
    directory.mkdir(parents=True,exist_ok=True)
    suffix="-once" if kind=="video-once" else ""
    path=directory / f"{rate}{suffix}.{kind if kind=='gif' else 'mkv'}"
    if path.exists(): return path
    if kind.startswith("video"):
        subprocess.run(["ffmpeg","-v","error","-f","lavfi","-i",f"testsrc2=size=120x120:rate={rate}","-frames:v", "7200" if kind=="video-once" else "180","-c:v","ffv1","-threads","1",str(path)],check=True)
    else:
        frames=[]
        for index in range(200):
            frame=Image.new("RGB",(120,120),(index*7%256,index*11%256,index*17%256))
            ImageDraw.Draw(frame).rectangle((index%92,20,index%92+28,90),fill=(240,240,240))
            frames.append(frame)
        frames[0].save(path,save_all=True,append_images=frames[1:],duration=1000//rate,loop=0,optimize=False)
    return path

def prepare(data, model, media, looping=True, font_family="DejaVu Sans"):
    (data/"pages").mkdir(parents=True)
    (data/"settings").mkdir()
    (data/".skip-onboarding").touch()
    rows,cols,_=MODELS[model]
    page={"keys":{},"dials":{},"settings":{}}
    for y in range(rows):
        for x in range(cols):
            page["keys"][f"{x}x{y}"]={"states":{"0":{"actions":[],"media":{"path":str(media),"fps":120,"loop":looping},"labels":{"center":{"text":f"Key {y*cols+x+1}","font-family":font_family,"font-size":14,"color":[255,255,255,255]}}}}}
    (data/"pages/Bench.json").write_text(json.dumps(page))
    serial="FAKE-"+{"plus-xl":"PLUSXL","ulanzi":"ULANZID200","mirabox":"MIRABOX293S"}.get(model,model.upper())+"-0"
    (data/"settings/native.json").write_text(json.dumps({"devices":{serial:{"page":"Bench","brightness":75,"screensaver":{"enable":False}}},"auto_lock":False,"cache_mib":64}))

def validate(data, model, seconds):
    first=native_status(data)["devices"][0]
    before={}; changes={}; sizes={}; start=time.monotonic()
    if first.get("tile_updates") is not None:
        # Two snapshots avoid aliasing and do not continuously contend with rendering.
        time.sleep(seconds)
        state=native_status(data); device=state["devices"][0]
        if state["errors"]: raise RuntimeError(state["errors"])
        for tile in device["frame_tiles"]:
            key=str(tile["key"]); sizes[key]=[tile["width"],tile["height"]]
        elapsed=(device["frame_clock_us"]-first["frame_clock_us"])/1e6
        changes={key:device["tile_updates"].get(key,0)-first["tile_updates"].get(key,0) for key in sizes}
        method="two changed-tile counter snapshots over the engine monotonic clock"
        polling=None
    else:
        while time.monotonic()-start < seconds:
            state=native_status(data)
            if state["errors"]: raise RuntimeError(state["errors"])
            device=state["devices"][0]
            for tile in device["frame_tiles"]:
                key=str(tile["key"]); sizes[key]=[tile["width"],tile["height"]]
                if key in before and before[key]!=tile["identity"]: changes[key]=changes.get(key,0)+1
                before[key]=tile["identity"]
            time.sleep(0.001)
        elapsed=time.monotonic()-start
        method="observed pixel identities (legacy socket limited to ~50 polls/s; renderer below 30 FPS)"
        polling=1
    rows,cols,size=MODELS[model]
    assert len(changes)==rows*cols and all(value>0 for value in changes.values()),(model,changes)
    assert all(value==list(size) for value in sizes.values()),sizes
    fps={key:value/elapsed for key,value in changes.items()}
    return {"seconds":elapsed,"method":method,"poll_interval_ms":polling,"fps_per_key":fps,"fps_min":min(fps.values()),"fps_max":max(fps.values()),"sizes":sizes,"written_tiles":device.get("written_tiles"),"instrumented":True}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before",type=pathlib.Path,default=ROOT/"target/benchmarks/deckard-v0.5.0-endpoint-baseline")
    parser.add_argument("--after",type=pathlib.Path,default=ROOT/"target/release/deckard")
    parser.add_argument("--versions",nargs="+",choices=["before","after"],default=["before","after"])
    parser.add_argument("--cases",nargs="+",choices=CASES,default=["plus10","plus50","plus100","plus60video","xl50","plusxl50"])
    parser.add_argument("--output",type=pathlib.Path,required=True)
    parser.add_argument("--cgroup-parent",type=pathlib.Path)
    parser.add_argument("--warmup",type=float,default=15)
    parser.add_argument("--duration",type=float,default=15)
    parser.add_argument("--validate-seconds",type=float,default=4)
    parser.add_argument("--trials",type=int,default=3)
    parser.add_argument("--visible",action="store_true")
    parser.add_argument("--gtk-renderer", choices=["auto", "gl", "vulkan", "cairo"], default="auto")
    parser.add_argument("--clear-glx-vendor", action="store_true", help="Diagnostic: remove a forced GLX vendor hint from the child environment")
    parser.add_argument("--disable-gpu", action="store_true", help="Diagnostic: disable GTK GL/Vulkan probing; use with Cairo")
    parser.add_argument("--font-family",default="DejaVu Sans")
    parser.add_argument("--weston-bundle",type=pathlib.Path,default=ROOT/"target/benchmarks/weston")
    args=parser.parse_args(); args.output=args.output.resolve(); args.output.mkdir(parents=True,exist_ok=True)
    args.wayland_runtime=pathlib.Path("/tmp/deckard-endpoint-wayland"); args.wayland_socket="deckard-endpoint"
    metadata={"date":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"platform":platform.platform(),"warmup":args.warmup,"duration":args.duration,"trials":args.trials,"visible":args.visible,"gtk_renderer":args.gtk_renderer,"gtk_a11y":"none","gtk_gpu_disabled":args.disable_gpu,"glx_vendor_hint":None if args.clear_glx_vendor else os.environ.get("__GLX_VENDOR_LIBRARY_NAME"),"requested_font":args.font_family,"cpu_unit":"100% = one logical core","memory_unit":"MiB PSS, application plus descendants","hardware_usb":False,"git_head":subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),"git_dirty":bool(subprocess.check_output(["git","status","--porcelain"],cwd=ROOT)),"binaries":{v:{"path":str(getattr(args,v)),"sha256":hashlib.sha256(getattr(args,v).read_bytes()).hexdigest()} for v in args.versions}}
    (args.output/"metadata.json").write_text(json.dumps(metadata,indent=2)); results=[]
    # Prepare fixtures before timing so media generation cannot affect CPU samples.
    assets={case:asset(args.output/"assets",rate,kind) for case,(model,rate,kind) in CASES.items() if case in args.cases}
    for trial in range(args.trials):
        for case in args.cases:
            model,rate,kind=CASES[case]
            versions=args.versions if trial%2==0 else list(reversed(args.versions))
            for version in versions:
                name=f"{version}-{case}-{trial+1}"; data=args.output/name; prepare(data,model,assets[case],looping=kind!="video-once",font_family=args.font_family)
                compositor=start_compositor(args,name) if args.visible else None
                bus=None; process=None; accounting=None
                try:
                    config=args.output/"session.conf"
                    config.write_text('<busconfig><type>session</type><listen>unix:tmpdir=/tmp</listen><policy context="default"><allow own="*"/><allow send_destination="*"/><allow receive_sender="*"/></policy></busconfig>')
                    bus=subprocess.Popen(["dbus-daemon",f"--config-file={config}","--nofork","--print-address=1"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,start_new_session=True)
                    address=bus.stdout.readline().strip()
                    home=data/"home"; home.mkdir()
                    env=os.environ.copy(); env.update(HOME=str(home),XDG_CONFIG_HOME=str(home/".config"),XDG_DATA_HOME=str(home/".local/share"),XDG_CACHE_HOME=str(home/".cache"),DBUS_SESSION_BUS_ADDRESS=address)
                    env.pop("DISPLAY",None); env.pop("DECKARD_RENDERER",None); env.pop("WGPU_BACKEND",None)
                    env.pop("GSK_RENDERER",None); env.pop("GDK_DISABLE",None)
                    env["GTK_A11Y"]="none"
                    if args.clear_glx_vendor: env.pop("__GLX_VENDOR_LIBRARY_NAME",None)
                    if args.gtk_renderer != "auto": env["GSK_RENDERER"]=args.gtk_renderer
                    if args.disable_gpu: env["GDK_DISABLE"]="gl,vulkan"
                    env.update(XDG_RUNTIME_DIR=str(args.wayland_runtime),WAYLAND_DISPLAY=args.wayland_socket,XDG_SESSION_TYPE="wayland")
                    command=[str(getattr(args,version)),"--skip-load-hardware-decks","--fake-deck-model",{"ulanzi":"ulanzi-d200","mirabox":"mirabox-293s"}.get(model,model),"--data",str(data)]
                    if not args.visible: command.append("-b")
                    with (args.output/f"{name}.log").open("w") as log:
                        accounting=CpuAccounting(args.cgroup_parent)
                        process=subprocess.Popen(accounting.command(command),env=env,stdout=log,stderr=log,start_new_session=True)
                        time.sleep(args.warmup)
                        if process.poll() is not None: raise RuntimeError(f"{name} failed startup")
                        state=native_status(data)
                        if state["errors"]: raise RuntimeError(state["errors"])
                        root=psutil.Process(process.pid); previous=accounting.read(); start=time.monotonic(); last=start; accumulated=0; samples=[]
                        while time.monotonic()-start<args.duration:
                            time.sleep(min(1,args.duration-(time.monotonic()-start)))
                            pss,rss,count,threads=memory_snapshot(root,accounting); current=accounting.read(); now=time.monotonic(); delta=current-previous
                            if delta<0: raise RuntimeError("kernel CPU counter decreased")
                            accumulated+=delta
                            samples.append({"elapsed":now-start,"cpu_percent":100*delta/(now-last),"pss_mib":pss,"rss_mib":rss,"processes":count,"threads":threads,"cpu_seconds":current}); previous=current;last=now
                        result={"version":version,"case":case,"model":model,"source_fps":rate,"requested_fps":120,"kind":kind,"trial":trial+1,"cpu_percent":100*accumulated/(last-start),"pss_mib":statistics.median(s["pss_mib"] for s in samples),"samples":samples,"timing_instrumented":False,"cpu_accounting":CpuAccounting.method}
                        # Mapping inspection and frame validation are AFTER CPU/PSS timing.
                        result["memory_mappings"]=[{"path":m.path,"pss_mib":m.pss/2**20} for m in root.memory_maps(grouped=True)]
                        result["validation"]=validate(data,model,args.validate_seconds)
                        results.append(result); (args.output/"results.json").write_text(json.dumps(results,indent=2))
                        print(f"{name}: {result['cpu_percent']:.3f}% CPU, {result['pss_mib']:.1f} MiB, {result['validation']['fps_min']:.1f}–{result['validation']['fps_max']:.1f} FPS",flush=True)
                finally:
                    if compositor is not None: stop(compositor)
                    if process is not None: stop(process)
                    if accounting is not None: accounting.close()
                    if bus is not None: stop(bus)
    summary=[]
    for case in args.cases:
        row={"case":case}
        for version in args.versions:
            subset=[r for r in results if r["case"]==case and r["version"]==version]
            cpu=statistics.median(r["cpu_percent"] for r in subset);fps=statistics.median(r["validation"]["fps_min"] for r in subset)
            row[version]={"cpu_percent":cpu,"pss_mib":statistics.median(r["pss_mib"] for r in subset),"fps":fps,"cpu_ms_per_device_frame":cpu*10/fps,"cpu_range":[min(r["cpu_percent"] for r in subset),max(r["cpu_percent"] for r in subset)]}
        summary.append(row)
    (args.output/"summary.json").write_text(json.dumps(summary,indent=2))

if __name__=="__main__": main()
