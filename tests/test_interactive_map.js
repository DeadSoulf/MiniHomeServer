/* Pan, wheel/pinch zoom and building footprint tests on a tiny DOM fixture. */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"),"utf8");
const init = source.lastIndexOf("\ninstallHandlers();");
assert.ok(init>0);

function makeHarness() {
  const elements = new Map();
  const classes=()=>({add(){},remove(){},toggle(){}});
  const node=(tag="div")=>({
    tag,style:{setProperty(key,value){this[key]=value;}},
    dataset:{},classList:classes(),children:[],attributes:{},
    clientWidth:0,clientHeight:0,hidden:false,
    addEventListener(name,fn){this.listeners??={};this.listeners[name]=fn;},
    removeEventListener(){},
    append(child){this.children.push(child);},
    replaceChildren(){this.children=[];},
    setAttribute(k,value){this.attributes[k]=String(value);},
    removeAttribute(k){delete this.attributes[k];},
    closest(){return null},
  });
  const viewport=node();viewport.clientWidth=1000;viewport.clientHeight=600;
  viewport.getBoundingClientRect=()=>({left:0,top:0,width:1000,height:600,
                                      right:1000,bottom:600});
  viewport.setPointerCapture=()=>{};viewport.hasPointerCapture=()=>false;
  viewport.releasePointerCapture=()=>{};
  elements.set("map-viewport",viewport);
  const stage=node();stage.clientWidth=1000;stage.clientHeight=600;
  elements.set("map-stage",stage);
  for(const id of ["map-zoom-in","map-zoom-out","map-zoom-reset","map-zoom-label",
                   "map-image","map-empty","map-camera-coverage","map-buildings",
                   "map-markers","edit-map-btn","map-mode-label"])elements.set(id,node());
  const doc={
    getElementById(id){return elements.get(id)||null;},
    createElement:node,
    createElementNS(_ns,tag){return node(tag);},
  };
  const context=vm.createContext({document:doc,Math,console,setTimeout(){}});
  stage.getBoundingClientRect=()=>{
    const zoom=vm.runInContext("mapZoom",context);
    const panX=vm.runInContext("mapPanX",context);
    const panY=vm.runInContext("mapPanY",context);
    return {left:panX,top:panY,width:1000*zoom,height:600*zoom,
            right:panX+1000*zoom,bottom:panY+600*zoom};
  };
  vm.runInContext(source.slice(0,init),context);
  return {context,elements,viewport,stage,node};
}
function evaluate(h,expression){return vm.runInContext(expression,h.context);}

test("zoom at cursor keeps the map point fixed, reset restores fit",()=>{
  const h=makeHarness();
  const original=evaluate(h,"mapPosition({clientX:250,clientY:180})");
  evaluate(h,"zoomMap(2,250,180)");
  const after=evaluate(h,"mapPosition({clientX:250,clientY:180})");
  assert.equal(original.x,0.25);
  assert.equal(original.y,0.3);
  assert.equal(after.x,original.x);
  assert.equal(after.y,original.y);
  assert.equal(h.stage.style.transform.includes("scale(2)"),true);
  assert.equal(h.elements.get("map-zoom-label").textContent,"200%");
  evaluate(h,"resetMapView()");
  assert.equal(evaluate(h,"mapZoom"),1);
  assert.equal(evaluate(h,"mapPanX"),0);
  assert.equal(evaluate(h,"mapPanY"),0);
});

test("zoom is limited from 50% to 500%",()=>{
  const h=makeHarness();
  assert.equal(evaluate(h,"clampMapZoom(0.001)"),0.5);
  assert.equal(evaluate(h,"clampMapZoom(99)"),5);
  evaluate(h,"zoomMap(0.1)");
  assert.equal(evaluate(h,"mapZoom"),0.5);
  evaluate(h,"zoomMap(60)");
  assert.equal(evaluate(h,"mapZoom"),5);
});

test("drag background pans without creating objects",()=>{
  const h=makeHarness();
  evaluate(h,"setupMapGestures()");
  const target={closest(){return null;}};
  h.viewport.listeners.pointerdown({pointerId:1,pointerType:"mouse",button:0,
                                    clientX:200,clientY:200,target});
  h.viewport.listeners.pointermove({pointerId:1,clientX:230,clientY:240});
  assert.equal(evaluate(h,"mapPanX"),30);
  assert.equal(evaluate(h,"mapPanY"),40);
  h.viewport.listeners.pointerup({pointerId:1});
  assert.ok(evaluate(h,"suppressMapClickUntil")>Date.now());
  assert.equal(evaluate(h,"mapPointers.size"),0);
});

test("two-finger pinch increases zoom",()=>{
  const h=makeHarness();
  evaluate(h,"setupMapGestures()");
  const target={closest(){return null;}};
  h.viewport.listeners.pointerdown({pointerId:1,pointerType:"touch",button:0,
                                    clientX:100,clientY:200,target});
  h.viewport.listeners.pointerdown({pointerId:2,pointerType:"touch",button:0,
                                    clientX:200,clientY:200,target});
  h.viewport.listeners.pointermove({pointerId:2,clientX:300,clientY:200});
  assert.equal(evaluate(h,"mapZoom"),2);
  h.viewport.listeners.pointerup({pointerId:1});
  h.viewport.listeners.pointerup({pointerId:2});
  assert.equal(evaluate(h,"mapPointers.size"),0);
});

test("building footprint appears once; existing house remains a marker",()=>{
  const h=makeHarness();
  h.context.buildingsJSON=JSON.stringify([
    {id:"new",name:"Гараж",kind:"building",x:0.35,y:0.6,building_shape:1,
     building_width:24,building_height:15,building_rotation:90,state:"unknown"},
    {id:"old",name:"Дом",kind:"house",x:0.7,y:0.25,building_shape:0,
     building_width:15,building_height:12,building_rotation:0,state:"unknown"}
  ]);
  evaluate(h,"state.devices=JSON.parse(buildingsJSON);renderMap();");
  const layer=h.elements.get("map-buildings");
  assert.equal(layer.children.length,1);
  const building=layer.children[0];
  assert.equal(building.dataset.id,"new");
  assert.equal(building.style.width,"24%");
  assert.equal(building.style.height,"15%");
  assert.equal(building.style["--building-angle"],"90deg");
  const markers=h.elements.get("map-markers");
  assert.equal(markers.children.length,1);
  assert.equal(markers.children[0].dataset.id,"old");
});
