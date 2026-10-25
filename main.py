from machine import Pin, I2C, PWM
import time
import mpr121

# ==============================================================================
# 1. CONFIGURACIÓN DE HARDWARE Y PINES
# ==============================================================================

# --- Bus I2C y Sensor MPR121 (Piano) ---
# Frecuencia a 100 kHz según la guía (GPIO21 = SDA, GPIO22 = SCL)
i2c = I2C(0, sda=Pin(21), scl=Pin(22), freq=100000)

# Escáner I2C integrado para evidencia inicial
dispositivos = i2c.scan()
print("--- Escáner I2C ---")
if dispositivos:
    for d in dispositivos:
        print(f"Dispositivo I2C detectado en dirección: {hex(d)}")
else:
    print("Error: No se detectó el MPR121. Revise conexiones y alimentación.")

# Inicializar MPR121 en 0x5A
mpr = mpr121.MPR121(i2c, 0x5A)

# Opcional: Ajuste de umbrales (Toque = 12, Liberación = 6)
# Si la librería de mcauser que descargaste no incluye este método, comenta la línea:
try:
    mpr.set_thresholds(12, 6)
except AttributeError:
    pass

# --- Piezo Pasivo (Salida PWM) ---
buzzer = PWM(Pin(25))
buzzer.duty(0) # Iniciar en silencio

# Frecuencias en Hz: Do4, Re4, Mi4, Fa4, Sol4, La4, Si4, Do5
NOTAS = [261, 293, 329, 349, 392, 440, 493, 523]
NOMBRES_NOTAS = ["Do4", "Re4", "Mi4", "Fa4", "Sol4", "La4", "Si4", "Do5"]

# --- Sensor TTP229-BSF (Cerradura) ---
scl_ttp = Pin(18, Pin.OUT)
sdo_ttp = Pin(19, Pin.IN)
scl_ttp.value(1) # Reposo en ALTO para interfaz activa-baja

# --- Indicadores LED ---
led_rojo = Pin(26, Pin.OUT)
led_verde = Pin(27, Pin.OUT) # Movido a GPIO27 para no chocar con el piezo en GPIO25
led_rojo.value(0)
led_verde.value(0)

# ==============================================================================
# 2. VARIABLES DE LA MÁQUINA DE ESTADOS Y TEMPORIZADORES
# ==============================================================================

N_TECLAS = 8
CLAVE_MAESTRA = [1, 2, 3, 4]
TECLA_BLOQUEO = 8 # Tecla para volver a bloquear el sistema manualmente

buffer_clave = []
piano_bloqueado = True
esperando_liberacion_total = False

# Control de estados previos
tecla_ttp_anterior = 0
candidato_tecla_ttp = 0
mascara_piano_anterior = 0
nota_sonando = -1

# Temporizadores no bloqueantes (ticks_ms)
t_ultima_trama_ttp = time.ticks_ms()
t_debounce_ttp = time.ticks_ms()
t_ultima_pulsacion = time.ticks_ms()
t_apagar_led_rojo = 0
led_rojo_activo = False

print("\n=== SISTEMA INICIADO ===")
print("Estado: PIANO BLOQUEADO. Ingrese la clave (1-2-3-4) en el TTP229.")

# ==============================================================================
# 3. FUNCIONES DE LECTURA Y CONTROL
# ==============================================================================

def leer_trama_ttp229():
    """
    Realiza bit-banging sobre el TTP229-BSF (interfaz activa-baja).
    Retorna el número de tecla (1 a 8), 0 si no hay toque, o -1 si hay toque múltiple.
    """
    mascara_activa = 0
    for i in range(N_TECLAS):
        scl_ttp.value(0)
        time.sleep_us(10) # Pulso de reloj bajo
        
        if sdo_ttp.value() == 0: # Activa-baja: 0 indica electrodo tocado
            mascara_activa |= (1 << i)
            
        scl_ttp.value(1)
        time.sleep_us(10) # Pulso de reloj alto
        
    if mascara_activa == 0:
        return 0
        
    # Verificar si hay más de un bit activo (múltiples teclas a la vez)
    if (mascara_activa & (mascara_activa - 1)) != 0:
        return -1 # La guía exige rechazar máscaras con múltiples teclas
        
    # Encontrar cuál fue la única tecla presionada (1 a 8)
    for i in range(N_TECLAS):
        if mascara_activa & (1 << i):
            return i + 1
    return 0

def actualizar_piano(mascara_actual):
    """
    Registra TOQUE y LIBRE por consola y aplica prioridad monofónica al piezo.
    """
    global mascara_piano_anterior, nota_sonando
    
    # 1. Detectar cambios individuales por electrodo para evidencia en consola
    if mascara_actual != mascara_piano_anterior:
        for i in range(8):
            bit_actual = (mascara_actual >> i) & 1
            bit_anterior = (mascara_piano_anterior >> i) & 1
            
            if bit_actual and not bit_anterior:
                print(f"[Piano] Electrodo E{i} ({NOMBRES_NOTAS[i]}): TOQUE")
            elif not bit_actual and bit_anterior:
                print(f"[Piano] Electrodo E{i} ({NOMBRES_NOTAS[i]}): LIBRE")
        mascara_piano_anterior = mascara_actual

    # 2. Control monofónico con prioridad al índice menor
    electrodo_prioritario = -1
    for i in range(8):
        if (mascara_actual >> i) & 1:
            electrodo_prioritario = i
            break # Se detiene en el primer electrodo activo que encuentre (menor índice)
            
    if electrodo_prioritario != -1:
        if nota_sonando != electrodo_prioritario:
            buzzer.freq(NOTAS[electrodo_prioritario])
            buzzer.duty(512) # 50% duty cycle
            nota_sonando = electrodo_prioritario
    else:
        if nota_sonando != -1:
            buzzer.duty(0)
            nota_sonando = -1

# ==============================================================================
# 4. BUCLE PRINCIPAL (NO BLOQUEANTE)
# ==============================================================================

while True:
    ahora = time.ticks_ms()
    
    # --------------------------------------------------------------------------
    # A. GESTIÓN DE TEMPORIZADORES (LED Rojo e Inactividad de 5s)
    # --------------------------------------------------------------------------
    if led_rojo_activo and time.ticks_diff(ahora, t_apagar_led_rojo) >= 0:
        led_rojo.value(0)
        led_rojo_activo = False
        
    if len(buffer_clave) > 0 and time.ticks_diff(ahora, t_ultima_pulsacion) > 5000:
        print("[Cerradura] Tiempo de espera agotado (>5s). Entrada parcial borrada.")
        buffer_clave.clear()

    # --------------------------------------------------------------------------
    # B. LECTURA Y LÓGICA DEL TTP229-BSF (CERRADURA)
    # --------------------------------------------------------------------------
    # Respetar separación entre tramas mayor a 2 ms (usamos 5 ms por seguridad)
    if time.ticks_diff(ahora, t_ultima_trama_ttp) >= 5:
        t_ultima_trama_ttp = ahora
        lectura_cruda = leer_trama_ttp229()
        
        # Filtro antirrebote (debounce) de 30 ms
        if lectura_cruda != candidato_tecla_ttp:
            candidato_tecla_ttp = lectura_cruda
            t_debounce_ttp = ahora
        elif time.ticks_diff(ahora, t_debounce_ttp) >= 30:
            # El estado ha sido estable por 30 ms
            if lectura_cruda != tecla_ttp_anterior:
                tecla_ttp_anterior = lectura_cruda
                
                # Exigencia de liberar todas las teclas tras un resultado
                if esperando_liberacion_total:
                    if lectura_cruda == 0:
                        esperando_liberacion_total = False
                        
                # Si se detectan varias teclas a la vez
                elif lectura_cruda == -1:
                    print("[Cerradura] Toque múltiple rechazado.")
                    
                # Si se presiona una tecla válida (flanco de subida, evita repetición por tecla sostenida)
                elif lectura_cruda > 0:
                    t_ultima_pulsacion = ahora
                    
                    # Caso 1: El piano ya está desbloqueado y se presiona la tecla de bloqueo (8)
                    if not piano_bloqueado and lectura_cruda == TECLA_BLOQUEO:
                        piano_bloqueado = True
                        led_verde.value(0)
                        buzzer.duty(0)
                        nota_sonando = -1
                        buffer_clave.clear()
                        esperando_liberacion_total = True
                        print("\n=== SISTEMA BLOQUEADO MANUALMENTE ===")
                        
                    # Caso 2: El piano está bloqueado y el usuario está ingresando la clave
                    elif piano_bloqueado:
                        buffer_clave.append(lectura_cruda)
                        print(f"[Cerradura] Tecla presionada: {lectura_cruda} | Progreso: {buffer_clave}")
                        
                        # Validar cuando se completan los 4 dígitos
                        if len(buffer_clave) == 4:
                            if buffer_clave == CLAVE_MAESTRA:
                                piano_bloqueado = False
                                led_verde.value(1) # Queda encendido indicando sistema activo
                                led_rojo.value(0)
                                led_rojo_activo = False
                                print("\n=== CLAVE CORRECTA: Piano listo y habilitado ===")
                            else:
                                print("\n[Cerradura] CLAVE INCORRECTA.")
                                led_rojo.value(1)
                                led_rojo_activo = True
                                t_apagar_led_rojo = time.ticks_add(ahora, 1000) # Encender por 1 segundo
                                
                            buffer_clave.clear()
                            esperando_liberacion_total = True

    # --------------------------------------------------------------------------
    # C. LECTURA Y LÓGICA DEL MPR121 (PIANO)
    # --------------------------------------------------------------------------
    if not piano_bloqueado:
        mascara_toques = mpr.touched()
        actualizar_piano(mascara_toques)
    else:
        # Asegurar que el piezo esté en silencio si se bloquea mientras sonaba una nota
        if nota_sonando != -1:
            buzzer.duty(0)
            nota_sonando = -1