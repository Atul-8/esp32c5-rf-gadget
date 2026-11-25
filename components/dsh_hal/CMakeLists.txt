idf_component_register(
    SRCS "src/dsh_hal_idf.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_mm esp_lcd esp_driver_gpio esp_driver_spi esp_rom freertos log heap
)
