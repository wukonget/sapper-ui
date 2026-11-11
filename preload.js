const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("sapperUI", {
  pickEnvFile: () => ipcRenderer.invoke("pick-env-file"),
});