import io
import json
import math
import re
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pydeck as pdk
import requests
import streamlit as st
from shapely.geometry import shape
from shapely.ops import polygonize, unary_union

st.set_page_config(page_title="도시계획시설 개략사업비 산정기", page_icon="🛣️", layout="wide")
st.title("🛣️ 도시계획시설 개략사업비 산정기")
st.caption("구역계 업로드 → 편입필지 자동산정 → 토지보상비 + 건축물보상비 + 공사비")

# -----------------------------------------------------------------------------
# 기준값
# -----------------------------------------------------------------------------
INFRA_COST = {"도로": 173_000, "공원": 105_000, "녹지": 87_000}
INFRA_NOTICE = "국토교통부 고시 제2025-398호"
INFRA_PERIOD = "2025.06.10 ~ 2026.06.09"

BUILDING_BASE_COST = {
    "주거용": 860_000,
    "상업용": 860_000,
    "공업용": 840_000,
    "농수산용": 640_000,
    "문화·복지·교육용": 860_000,
    "공공용": 850_000,
}

MULT_COLS = ["전체", "주거·상업·공업", "녹지", "관리", "농림·자연보호", "주거용·공업용", "상업용·주차용", "전·답·과", "임야", "공공·기타"]
MULT_RAW = {
    "서울": [1.66,1.59,1.84,None,None,1.23,1.52,1.29,2.77,3.66],
    "부산": [1.90,1.87,1.93,None,None,1.86,1.61,1.90,3.00,3.90],
    "대구": [2.05,1.90,2.18,2.90,2.78,1.92,1.57,2.05,3.89,4.89],
    "인천": [2.10,1.66,1.77,3.13,2.36,1.66,1.11,2.16,2.64,3.89],
    "광주": [2.13,1.54,2.71,2.57,None,1.54,1.31,2.18,2.80,3.28],
    "대전": [1.59,1.59,1.83,2.00,3.00,1.59,1.57,1.60,2.59,3.81],
    "울산": [2.78,2.09,3.04,2.82,3.00,1.91,1.88,2.45,5.00,4.44],
    "세종": [2.87,2.55,2.79,3.33,2.75,2.34,2.04,2.70,5.11,4.16],
    "경기": [1.85,1.49,1.92,2.08,2.01,1.63,1.57,1.77,2.70,2.88],
    "강원": [2.44,1.89,2.65,2.71,2.68,1.90,1.64,2.38,4.46,4.62],
    "충북": [2.35,1.37,2.38,2.88,2.61,1.74,1.56,2.31,3.07,5.20],
    "충남": [2.49,1.93,2.54,2.96,2.39,2.04,1.63,2.33,3.58,4.06],
    "전북": [2.15,1.82,2.22,2.61,2.09,1.95,1.69,2.11,3.42,4.25],
    "전남": [2.50,2.03,2.75,2.62,2.47,2.17,1.72,2.40,4.50,5.00],
    "경북": [2.64,2.24,2.52,2.99,2.54,2.10,1.82,2.52,4.50,5.31],
    "경남": [2.73,1.96,3.08,3.13,2.62,2.13,1.80,2.70,4.50,4.17],
    "제주": [2.17,1.73,2.22,2.60,2.71,1.69,1.50,2.43,3.10,4.11],
}
MULT = {k: dict(zip(MULT_COLS, v)) for k, v in MULT_RAW.items()}
SIDO_FROM_PNU = {"11":"서울","26":"부산","27":"대구","28":"인천","29":"광주","30":"대전","31":"울산","36":"세종","41":"경기","42":"강원","51":"강원","43":"충북","44":"충남","45":"전북","52":"전북","46":"전남","47":"경북","48":"경남","50":"제주"}

VWORLD_DATA_URL = "https://api.vworld.kr/req/data"
VWORLD_LAND_URL = "https://api.vworld.kr/ned/data/ladfrlList"
VWORLD_CHAR_URL = "https://api.vworld.kr/ned/data/getLandCharacteristics"
VWORLD_PRICE_URL = "https://api.vworld.kr/ned/data/getIndvdLandPriceAttr"
BUILDING_HUB_URL = "https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo"


def money(v):
    v = float(v or 0)
    return f"{v/100_000_000:,.2f} 억원" if v >= 100_000_000 else f"{v:,.0f} 원"


def fnum(v, default=0.0):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return default


def first(d, keys, default=""):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", "null"):
            return d[k]
    return default


def deep_dicts(obj, keys):
    out = []
    def walk(x):
        if isinstance(x, dict):
            if any(k in x for k in keys): out.append(x)
            for v in x.values(): walk(v)
        elif isinstance(x, list):
            for v in x: walk(v)
    walk(obj)
    return out


def get_json(url, params, timeout=40):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        raise RuntimeError(r.text[:500])


def load_zone(uploaded, epsg):
    suffix = Path(uploaded.name).suffix.lower()
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        src = td / uploaded.name
        src.write_bytes(uploaded.getvalue())
        if suffix == ".zip":
            out = td / "unzipped"; out.mkdir()
            with zipfile.ZipFile(src) as z: z.extractall(out)
            shp = list(out.rglob("*.shp"))
            if not shp: raise ValueError("ZIP 안에 SHP 파일이 없습니다.")
            gdf = gpd.read_file(shp[0])
        else:
            gdf = gpd.read_file(src)
    if gdf.empty: raise ValueError("도형이 없습니다.")
    if gdf.crs is None: gdf = gdf.set_crs(f"EPSG:{epsg}")
    poly = gdf[gdf.geometry.geom_type.isin(["Polygon","MultiPolygon"])]
    if len(poly):
        geom = unary_union(poly.geometry.tolist())
    else:
        geom = unary_union(list(polygonize(unary_union(gdf.geometry.tolist()))))
    if geom.is_empty: raise ValueError("닫힌 구역계 면을 만들 수 없습니다.")
    return gpd.GeoDataFrame({"name":["구역계"]}, geometry=[geom], crs=gdf.crs).to_crs(4326)


def area_m2(gdf):
    return float(gdf.to_crs(5179).geometry.area.sum())


def tile_bounds(bounds, step=0.02):
    minx,miny,maxx,maxy = bounds
    nx=max(1,math.ceil((maxx-minx)/step)); ny=max(1,math.ceil((maxy-miny)/step))
    if nx*ny>250: raise ValueError("조회 범위가 너무 큽니다. 구역계를 나눠주세요.")
    dx=(maxx-minx)/nx; dy=(maxy-miny)/ny
    for i in range(nx):
        for j in range(ny):
            yield (minx+i*dx,miny+j*dy,maxx if i==nx-1 else minx+(i+1)*dx,maxy if j==ny-1 else miny+(j+1)*dy)


def vworld_features(data_id, bounds, key, domain):
    feats=[]; seen=set()
    for box in tile_bounds(bounds):
        params={"service":"data","version":"2.0","request":"GetFeature","format":"json","errorformat":"json","size":"1000","page":"1","geometry":"true","attribute":"true","crs":"EPSG:4326","data":data_id,"geomfilter":f"BOX({box[0]},{box[1]},{box[2]},{box[3]})","key":key}
        if domain: params["domain"]=domain
        data=get_json(VWORLD_DATA_URL,params)
        rsp=data.get("response",{})
        if rsp.get("status")=="ERROR":
            e=rsp.get("error",{}); raise RuntimeError(f"VWorld 오류: {e.get('code','')} {e.get('text','')}")
        fc=rsp.get("result",{}).get("featureCollection",{})
        for f in fc.get("features",[]) or []:
            p=f.get("properties",{}); marker=(str(p.get("pnu","")),json.dumps(f.get("geometry"),sort_keys=True)[:300])
            if marker not in seen: seen.add(marker); feats.append(f)
    return feats


def features_gdf(features):
    rows=[]; geoms=[]
    for f in features:
        if f.get("geometry"):
            rows.append(f.get("properties",{})); geoms.append(shape(f["geometry"]))
    return gpd.GeoDataFrame(rows,geometry=geoms,crs=4326) if geoms else gpd.GeoDataFrame(geometry=[],crs=4326)


def ned(url,pnu,key,domain,year=None):
    params={"pnu":pnu,"format":"json","key":key,"numOfRows":"100","pageNo":"1"}
    if domain: params["domain"]=domain
    if year: params["stdrYear"]=str(year)
    return get_json(url,params)


def parcel_info(pnu,key,domain):
    land={}; char={}; price={}
    try:
        rows=deep_dicts(ned(VWORLD_LAND_URL,pnu,key,domain),{"lndpclAr","lndcgrCodeNm"}); land=rows[0] if rows else {}
    except Exception: pass
    for y in range(date.today().year,date.today().year-6,-1):
        try:
            rows=deep_dicts(ned(VWORLD_CHAR_URL,pnu,key,domain,y),{"landUseSittnCodeNm","lndUseSittnCodeNm","prposArea1Nm"})
            if rows: char=rows[0]; break
        except Exception: pass
    for y in range(date.today().year,date.today().year-6,-1):
        try:
            rows=deep_dicts(ned(VWORLD_PRICE_URL,pnu,key,domain,y),{"pblntfPclnd","stdrYear"})
            rows=[r for r in rows if first(r,["pblntfPclnd"],"") not in ("",None)]
            if rows: price=rows[0]; break
        except Exception: pass
    landcat=first(land,["lndcgrCodeNm"],"")
    usage=first(char,["landUseSittnCodeNm","lndUseSittnCodeNm"],"")
    zoning=" / ".join([x for x in [first(char,["prposArea1Nm"],""),first(char,["prposArea2Nm"],"")] if x])
    text=f"{usage} {landcat}"
    if any(k in text for k in ["주거","공업","공장"]): cat="주거용·공업용"
    elif any(k in text for k in ["상업","주차"]): cat="상업용·주차용"
    elif any(k in text for k in ["전","답","과수"]): cat="전·답·과"
    elif any(k in text for k in ["임야","산림"]): cat="임야"
    else: cat="공공·기타"
    sido=SIDO_FROM_PNU.get(pnu[:2],""); mult=MULT.get(sido,{}).get(cat) or MULT.get(sido,{}).get("전체",1.0)
    return {"PNU":pnu,"시도":sido,"지목":landcat,"이용상황":usage,"용도지역":zoning,"공시지가(원/㎡)":fnum(first(price,["pblntfPclnd"],0)),"배율구분":cat,"보상배율":mult}


def bld_purpose(text):
    if any(k in text for k in ["단독주택","공동주택","주거"]): return "주거용"
    if any(k in text for k in ["공장","제조","산업"]): return "공업용"
    if any(k in text for k in ["축사","농수산","농업"]): return "농수산용"
    if any(k in text for k in ["학교","교육","의료","문화","복지","종교"]): return "문화·복지·교육용"
    if any(k in text for k in ["공공","군사","국방"]): return "공공용"
    return "상업용"


def dep_rate(structure):
    if "철골철근콘크리트" in structure: return 0.018
    if "철근콘크리트" in structure: return 0.0225
    if "철골" in structure: return 0.03
    if any(k in structure for k in ["벽돌","블록","목조","조립식"]): return 0.045
    return 0.03


def buildings_for_pnu(pnu,key):
    if not key: return []
    sig=pnu[:5]; bj=pnu[5:10]; flag="1" if pnu[10]=="2" else "0"; bun=pnu[11:15]; ji=pnu[15:19]
    params={"serviceKey":key,"sigunguCd":sig,"bjdongCd":bj,"platGbCd":flag,"bun":bun,"ji":ji,"pageNo":"1","numOfRows":"100","_type":"json"}
    data=get_json(BUILDING_HUB_URL,params)
    rows=deep_dicts(data,{"mgmBldrgstPk","totArea","mainPurpsCdNm","strctCdNm","useAprDay"})
    return [r for r in rows if "mgmBldrgstPk" in r and "totArea" in r]


def map_view(zone,parcels=None):
    layers=[pdk.Layer("GeoJsonLayer",json.loads(zone.to_json()),stroked=True,filled=True,opacity=0.22,get_line_width=3)]
    if parcels is not None and not parcels.empty:
        layers.append(pdk.Layer("GeoJsonLayer",json.loads(parcels.to_crs(4326).to_json()),stroked=True,filled=False,get_line_width=1))
    c=zone.geometry.iloc[0].centroid
    st.pydeck_chart(pdk.Deck(layers=layers,initial_view_state=pdk.ViewState(latitude=c.y,longitude=c.x,zoom=15),map_style=None),use_container_width=True)


def excel_bytes(summary,land,bld):
    bio=io.BytesIO()
    with pd.ExcelWriter(bio,engine="openpyxl") as w:
        summary.to_excel(w,"요약",index=False); land.to_excel(w,"토지보상",index=False); bld.to_excel(w,"건축물보상",index=False)
    return bio.getvalue()

vworld_key=st.secrets.get("VWORLD_API_KEY","")
vworld_domain=st.secrets.get("VWORLD_DOMAIN","")
bld_key=st.secrets.get("BUILDING_HUB_API_KEY","")

with st.sidebar:
    st.subheader("📐 원본 좌표계")
    epsg=st.text_input("DXF/SHP 원본 EPSG 번호",value="5179",help="QGIS에서 확인한 EPSG 번호를 입력하세요. 예: 5179, 5186")
    st.caption("SHP에 .prj가 있거나 GPKG에 CRS가 있으면 파일 CRS가 우선합니다.")
    st.divider(); st.subheader("🔑 API 상태")
    st.write(f"VWorld: {'✅' if vworld_key else '❌'}"); st.write(f"건축HUB: {'✅' if bld_key else '❌'}")

st.subheader("① 구역계 및 시설종류")
c1,c2,c3=st.columns([2.2,1,1])
with c1: uploaded=st.file_uploader("구역계 업로드",type=["zip","shp","geojson","json","gpkg","dxf"])
with c2: facility=st.selectbox("기반시설 종류",["도로","공원","녹지"])
with c3: st.text_input("적용 조성비",value=f"{INFRA_COST[facility]:,}원/㎡",disabled=True)
st.caption(f"{facility} 표준조성비 {INFRA_COST[facility]:,}원/㎡ · {INFRA_NOTICE} · 적용기간 {INFRA_PERIOD}")

zone=None
if uploaded:
    try:
        if not epsg.isdigit(): raise ValueError("EPSG는 숫자로 입력해 주세요.")
        zone=load_zone(uploaded,epsg)
        minx,miny,maxx,maxy=zone.total_bounds; lon=(minx+maxx)/2; lat=(miny+maxy)/2
        if not (123<=lon<=133.5 and 32<=lat<=40.5):
            st.error(f"좌표가 대한민국 범위를 벗어났습니다. EPSG:{epsg}가 맞는지 확인해 주세요. 중심좌표 {lon:.5f}, {lat:.5f}")
        else:
            st.success(f"좌표 확인 완료 · EPSG:{epsg} → WGS84 중심좌표 {lon:.5f}, {lat:.5f}")
        zarea=area_m2(zone)
        a1,a2=st.columns(2); a1.metric("구역계 면적",f"{zarea:,.2f} ㎡"); a2.metric("예상 공사비",money(zarea*INFRA_COST[facility]))
        map_view(zone)
    except Exception as e: st.error(f"구역계 읽기 실패: {e}")

if st.button("🧮 개략사업비 산정",type="primary",use_container_width=True,disabled=(zone is None or not vworld_key)):
    try:
        with st.spinner("연속지적도와 구역계를 교차하고 있습니다..."):
            cad=features_gdf(vworld_features("LP_PA_CBND_BUBUN",tuple(zone.total_bounds),vworld_key,vworld_domain))
        if cad.empty: raise RuntimeError("연속지적도를 찾지 못했습니다.")
        if "pnu" not in cad.columns:
            pc=next((c for c in cad.columns if c.lower()=="pnu"),None)
            if not pc: raise RuntimeError("PNU 필드가 없습니다.")
            cad["pnu"]=cad[pc]
        zm=zone.to_crs(5179).geometry.iloc[0]; cm=cad.to_crs(5179); cm=cm[cm.geometry.intersects(zm)].copy()
        cm["편입면적(㎡)"]=cm.geometry.intersection(zm).area; cm=cm[cm["편입면적(㎡)"]>0.01].copy(); cm["PNU"]=cm["pnu"].astype(str); cm=cm.drop_duplicates("PNU")
        basics=cm[["PNU","편입면적(㎡)"]].to_dict("records")
        rows=[]
        prog=st.progress(0,text="필지별 공시지가 조회 중")
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs={ex.submit(parcel_info,r["PNU"],vworld_key,vworld_domain):r for r in basics}
            for i,f in enumerate(as_completed(futs),1):
                r=futs[f]; info=f.result(); info["편입면적(㎡)"]=r["편입면적(㎡)"]; rows.append(info); prog.progress(i/len(futs),text=f"필지 조회 {i}/{len(futs)}")
        prog.empty(); land=pd.DataFrame(rows).sort_values("PNU").reset_index(drop=True); land.insert(0,"보상포함",True)
        bld_rows=[]
        for pnu in land["PNU"]:
            for r in buildings_for_pnu(pnu,bld_key):
                use=first(r,["mainPurpsCdNm","etcPurps"],""); structure=first(r,["strctCdNm","etcStrct"],""); area=fnum(first(r,["totArea"],0)); appr=str(first(r,["useAprDay"],"")); age=None
                if len(re.sub(r"\D","",appr))>=4: age=max(date.today().year-int(re.sub(r"\D","",appr)[:4]),0)
                cat=bld_purpose(use); residual=max(0.10,1-(age or 0)*dep_rate(structure))
                bld_rows.append({"보상포함":True,"PNU":pnu,"주용도":use,"용도분류":cat,"구조":structure,"연면적(㎡)":area,"사용승인일":appr,"경과연수":age,"기준단가(원/㎡)":BUILDING_BASE_COST[cat],"잔가율(%)":round(residual*100,1)})
        st.session_state["result"]={"zone":zone,"parcels":cm[["PNU","geometry"]].copy(),"land":land,"bld":pd.DataFrame(bld_rows),"facility":facility,"zarea":area_m2(zone)}
        st.success("산정용 데이터 조회가 완료되었습니다.")
    except Exception as e: st.exception(e)

res=st.session_state.get("result")
if res:
    st.divider(); st.subheader("② 토지보상비")
    land=st.data_editor(res["land"],hide_index=True,use_container_width=True,disabled=["PNU","시도","지목","이용상황","용도지역","배율구분"],key="landedit")
    land["토지보상비(원)"]=land["편입면적(㎡)"]*land["공시지가(원/㎡)"]*land["보상배율"]
    land_cost=float(land.loc[land["보상포함"]==True,"토지보상비(원)"].sum())
    st.metric("토지보상비",money(land_cost))

    st.subheader("③ 건축물보상비")
    bld=res["bld"]
    if bld.empty:
        st.info("조회된 건축물이 없습니다."); bld_cost=0.0; edited_bld=bld
    else:
        edited_bld=st.data_editor(bld,hide_index=True,use_container_width=True,disabled=["PNU","주용도","용도분류","구조","연면적(㎡)","사용승인일","경과연수"],key="bldedit")
        edited_bld["건축물보상비(원)"]=edited_bld["연면적(㎡)"]*edited_bld["기준단가(원/㎡)"]*(edited_bld["잔가율(%)"]/100)
        bld_cost=float(edited_bld.loc[edited_bld["보상포함"]==True,"건축물보상비(원)"].sum())
        st.metric("건축물보상비",money(bld_cost))

    st.subheader("④ 공사비")
    cc1,cc2=st.columns(2)
    with cc1: const_area=st.number_input("공사 적용면적(㎡)",min_value=0.0,value=float(round(res["zarea"],2)))
    with cc2: const_unit=st.number_input("시설별 표준조성비(원/㎡)",min_value=0,value=int(INFRA_COST[res["facility"]]),step=1000)
    const_cost=const_area*const_unit; st.metric("공사비",money(const_cost))

    st.subheader("⑤ 총 개략사업비")
    total=land_cost+bld_cost+const_cost
    x1,x2,x3,x4=st.columns(4); x1.metric("토지보상비",money(land_cost)); x2.metric("건축물보상비",money(bld_cost)); x3.metric("공사비",money(const_cost)); x4.metric("총 개략사업비",money(total))
    st.markdown("**개략사업비 = 토지보상비 + 건축물보상비 + 공사비**")
    with st.expander("🗺️ 편입필지 확인"): map_view(res["zone"],res["parcels"])
    summary=pd.DataFrame([["시설종류",res["facility"]],["구역계면적(㎡)",res["zarea"]],["토지보상비(원)",land_cost],["건축물보상비(원)",bld_cost],["공사비(원)",const_cost],["총 개략사업비(원)",total]],columns=["항목","값"])
    st.download_button("📥 결과 Excel 다운로드",excel_bytes(summary,land,edited_bld),file_name=f"개략사업비_{res['facility']}.xlsx",mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",use_container_width=True)
    st.warning("※ 본 결과는 검토 단계의 개략사업비입니다. 실제 보상액은 감정평가 및 사업시행 단계에서 달라질 수 있습니다.")
