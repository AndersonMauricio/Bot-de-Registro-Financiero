/**
 * Avisa al bot cuando un administrador edita la columna "Estado" de la pestaña "usuarios".
 *
 * El bot cachea la lista de usuarios en memoria; con este aviso la cambia al instante (sin esperar al TTL y
 * sin lecturas programadas a la API de Sheets). Instrucciones: docs/apps_script_usuarios.md
 *
 * Propiedades del script (Configuración del proyecto > Propiedades del script), nunca en el código:
 *   BOT_WEBHOOK_URL  URL pública del bot, ej. https://<tu-dominio>/api/v1/webhooks/sheets
 *   WEBHOOK_SECRET   el mismo valor que SHEETS_WEBHOOK_SECRET en el .env del bot
 */

const USERS_SHEET_NAME = 'usuarios';
const PHONE_HEADER = 'telefono';
const STATUS_HEADER = 'estado';
// Si una edición toca más filas que esto (pegado masivo), se manda un solo aviso "invalidar todo"
const MAX_ROWS_PER_EDIT = 20;

/**
 * Manejador del disparador instalable (un onEdit simple no puede llamar a UrlFetchApp).
 * Se registra una sola vez ejecutando instalarTrigger().
 */
function onEditHandler(e) {
  if (!e || !e.range) return;
  const range = e.range;
  const sheet = range.getSheet();
  if (sheet.getName() !== USERS_SHEET_NAME) return;

  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
    .map(function (h) { return String(h).trim().toLowerCase(); });
  const statusCol = headers.indexOf(STATUS_HEADER) + 1;
  const phoneCol = headers.indexOf(PHONE_HEADER) + 1;
  if (!statusCol || !phoneCol) {
    console.error('La pestaña "' + USERS_SHEET_NAME + '" necesita las columnas Telefono y Estado.');
    return;
  }

  // Solo importan las ediciones que tocan la columna Estado, y nunca la fila de encabezados
  if (statusCol < range.getColumn() || statusCol > range.getLastColumn()) return;
  const firstRow = Math.max(range.getRow(), 2);
  const lastRow = range.getLastRow();
  if (lastRow < firstRow) return;

  const rowCount = lastRow - firstRow + 1;
  if (rowCount > MAX_ROWS_PER_EDIT) {
    notifyBot_({ event: 'bulk_edit', sheet: USERS_SHEET_NAME });
    return;
  }

  const phones = sheet.getRange(firstRow, phoneCol, rowCount, 1).getValues();
  phones.forEach(function (cell) {
    const phone = String(cell[0]).replace(/\D/g, '');
    if (phone) notifyBot_({ event: 'estado_changed', sheet: USERS_SHEET_NAME, phone: phone });
  });
}

/** Registra el disparador instalable sobre esta hoja. Ejecutar UNA vez (pide autorización). */
function instalarTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(function (t) { return t.getHandlerFunction() === 'onEditHandler'; })
    .forEach(function (t) { ScriptApp.deleteTrigger(t); });
  ScriptApp.newTrigger('onEditHandler').forSpreadsheet(SpreadsheetApp.getActive()).onEdit().create();
  console.log('Disparador instalado: onEditHandler (al editar).');
}

/** Verifica la URL y el secreto sin tocar la caché del bot. Ver el resultado en Ejecuciones / Registro. */
function probarConexion() {
  console.log(notifyBot_({ event: 'ping' }) ? 'Conexión OK: el bot respondió.' : 'FALLÓ: revisá la URL, el secreto y el registro de errores.');
}

/** Fuerza al bot a releer toda la lista (por ejemplo, después de borrar filas o editar teléfonos). */
function invalidarTodo() {
  console.log(notifyBot_({ event: 'bulk_edit', sheet: USERS_SHEET_NAME }) ? 'Caché invalidada.' : 'FALLÓ el aviso.');
}

/** POST al bot con el secreto en el header. Reintenta ante fallos transitorios; no loguea secretos ni teléfonos. */
function notifyBot_(payload) {
  const props = PropertiesService.getScriptProperties();
  const url = props.getProperty('BOT_WEBHOOK_URL');
  const secret = props.getProperty('WEBHOOK_SECRET');
  if (!url || !secret) {
    console.error('Faltan las propiedades del script BOT_WEBHOOK_URL y/o WEBHOOK_SECRET.');
    return false;
  }

  const options = {
    method: 'post',
    contentType: 'application/json',
    headers: { 'x-webhook-secret': secret },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true,
  };
  const label = payload.event + (payload.phone ? ' (...' + payload.phone.slice(-4) + ')' : '');

  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const code = UrlFetchApp.fetch(url, options).getResponseCode();
      if (code >= 200 && code < 300) return true;
      console.error('El bot respondió HTTP ' + code + ' para ' + label + '.');
      if (code >= 400 && code < 500) return false; // 401/404/422: reintentar no arregla una mala configuración
    } catch (error) {
      console.error('No se pudo contactar al bot para ' + label + ': ' + error);
    }
    Utilities.sleep(attempt * 1000);
  }
  return false;
}
