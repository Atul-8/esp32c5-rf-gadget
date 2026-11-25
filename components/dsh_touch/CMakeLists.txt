idf_component_register(
    SRCS "src/dsh_touch_idf.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_driver_i2c esp_driver_gpio
)
