/**
 * Avisa al bot cuando un administrador edita la columna "Estado" de la pestaña "usuarios".
 *
 * El bot cachea la lista de usuarios en memoria; con este aviso la cambia al instante (sin esperar al TTL y
 * sin lecturas programadas a la API de Sheets) y además le escribe al cliente por WhatsApp para avisarle
 * que ya puede registrar gastos (estado 1) o que su solicitud no fue aprobada (estado 2).
 * Instrucciones y diagnóstico: docs/apps_script_usuarios.md
 *
 * Propiedades del script (Configuración del proyecto > Propiedades del script), nunca en el código:
 *   BOT_WEBHOOK_URL  URL pública del bot, ej. https://<tu-dominio>/api/v1/webhooks/sheets
 *   WEBHOOK_SECRET   el mismo valor que SHEETS_WEBHOOK_SECRET en el .env del bot
 */

const USERS_SHEET_NAME = 'usuarios';
const PHONE_HEADER = 'telefono';
const STATUS_HEADER = 'estado';
// Filas por POST: los lotes grandes viajan en varios pedidos cortos (Apps Script tiene presupuesto de ejecución)
const MAX_ROWS_PER_POST = 50;
// Si una edición toca más filas que esto (pegado masivo enorme), se manda un solo aviso "invalidar todo"
const MAX_ROWS_PER_EDIT = 500;

/**
 * Manejador del disparador instalable (un onEdit simple no puede llamar a UrlFetchApp).
 * Se registra una sola vez ejecutando instalarTrigger().
 */
function onEditHandler(e) {
  if (!e || !e.range) return;
  const range = e.range;
  const sheet = range.getSheet();
  if (sheet.getName() !== USERS_SHEET_NAME) return;

  const columns = columnIndexes_(sheet);
  if (!columns.status || !columns.phone) return;

  // Solo importan las ediciones que tocan la columna Estado, y nunca la fila de encabezados
  if (columns.status < range.getColumn() || columns.status > range.getLastColumn()) return;
  const firstRow = Math.max(range.getRow(), 2);
  const lastRow = range.getLastRow();
  if (lastRow < firstRow) return;

  const rowCount = lastRow - firstRow + 1;
  if (rowCount > MAX_ROWS_PER_EDIT) {
    notifyBot_({ event: 'bulk_edit', sheet: USERS_SHEET_NAME });
    return;
  }

  const phones = sheet.getRange(firstRow, columns.phone, rowCount, 1).getValues();
  const states = sheet.getRange(firstRow, columns.status, rowCount, 1).getValues();
  // e.oldValue solo existe cuando la edición es de una celda: evita repetir el aviso si se vuelve a
  // guardar el mismo valor en un usuario ya aprobado o rechazado
  const previous = rowCount === 1 ? e.oldValue : undefined;

  const changes = [];
  phones.forEach(function (cell, index) {
    const phone = digitsOf_(cell[0]);
    if (!phone) return;
    const change = { phone: phone, estado: states[index][0] };
    if (previous !== undefined) change.estado_anterior = previous;
    changes.push(change);
  });
  sendChanges_(changes);
}

/** Reenvía el aviso de todas las filas con Estado 1 o 2. Ejecutar UNA vez para los usuarios ya cargados. */
function reenviarAvisos() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(USERS_SHEET_NAME);
  if (!sheet) {
    console.error('No existe la pestaña "' + USERS_SHEET_NAME + '".');
    return;
  }
  const columns = columnIndexes_(sheet);
  if (!columns.status || !columns.phone) return;

  const lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    console.log('La pestaña está vacía: no hay a quién avisar.');
    return;
  }

  const rowCount = lastRow - 1;
  const phones = sheet.getRange(2, columns.phone, rowCount, 1).getValues();
  const states = sheet.getRange(2, columns.status, rowCount, 1).getValues();
  const changes = [];
  phones.forEach(function (cell, index) {
    const phone = digitsOf_(cell[0]);
    if (phone) changes.push({ phone: phone, estado: states[index][0] });
  });
  console.log('Enviando ' + sendChanges_(changes) + ' fila(s); el bot solo avisa a los usuarios aprobados o rechazados.');
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

/** Índices (1-based) de las columnas Telefono y Estado; 0 si falta alguna. */
function columnIndexes_(sheet) {
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0]
    .map(function (h) { return String(h).trim().toLowerCase(); });
  const columns = {
    status: headers.indexOf(STATUS_HEADER) + 1,
    phone: headers.indexOf(PHONE_HEADER) + 1,
  };
  if (!columns.status || !columns.phone) {
    console.error('La pestaña "' + USERS_SHEET_NAME + '" necesita las columnas Telefono y Estado.');
  }
  return columns;
}

/** Manda los cambios en lotes de MAX_ROWS_PER_POST. Devuelve cuántas filas se informaron. */
function sendChanges_(changes) {
  for (let start = 0; start < changes.length; start += MAX_ROWS_PER_POST) {
    notifyBot_({
      event: 'estado_changed',
      sheet: USERS_SHEET_NAME,
      changes: changes.slice(start, start + MAX_ROWS_PER_POST),
    });
  }
  return changes.length;
}

function digitsOf_(raw) {
  return String(raw).replace(/\D/g, '');
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
  const count = payload.changes ? payload.changes.length : (payload.phone ? 1 : 0);
  const label = payload.event + (count ? ' (' + count + ' fila' + (count === 1 ? '' : 's') + ')' : '');

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
