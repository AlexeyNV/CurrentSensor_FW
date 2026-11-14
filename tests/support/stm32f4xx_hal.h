#ifndef TEST_STM32F4_HAL_H
#define TEST_STM32F4_HAL_H

#include <stdint.h>

typedef struct { uint32_t unused; } SPI_HandleTypeDef;
typedef struct { uint32_t unused; } GPIO_TypeDef;
typedef struct { uint32_t CR1, PSC, ARR, CNT, EGR, SR, DIER; } TIM_TypeDef;
typedef struct { TIM_TypeDef *Instance; } TIM_HandleTypeDef;
typedef struct { uint32_t APB1CLKDivider; } RCC_ClkInitTypeDef;
typedef struct { uint32_t DEMCR; } CoreDebug_Type;
typedef struct { uint32_t CYCCNT, CTRL; } DWT_Type;
extern TIM_TypeDef fake_tim2, fake_tim3;
extern CoreDebug_Type fake_debug;
extern DWT_Type fake_dwt;
#define TIM2 (&fake_tim2)
#define TIM3 (&fake_tim3)
#define TIM_CR1_CEN 1U
#define CoreDebug (&fake_debug)
#define DWT (&fake_dwt)
#define TIM3_IRQn 3
#define TIM_CR1_CEN 1U
#define TIM_IT_UPDATE 1U
#define TIM_FLAG_UPDATE 1U
#define TIM_EGR_UG 1U
#define RCC_HCLK_DIV1 1U
#define CoreDebug_DEMCR_TRCENA_Msk 1U
#define DWT_CTRL_CYCCNTENA_Msk 1U
#define HAL_OK 0

uint32_t Test_TimerCounter(TIM_HandleTypeDef *timer);
#define __HAL_TIM_GET_COUNTER(t) Test_TimerCounter(t)
#define __HAL_TIM_SET_COUNTER(t, v) ((t)->Instance->CNT = (v))
#define __HAL_TIM_SET_PRESCALER(t, v) ((t)->Instance->PSC = (v))
#define __HAL_TIM_SET_AUTORELOAD(t, v) ((t)->Instance->ARR = (v))
#define __HAL_TIM_DISABLE_IT(t, v) ((t)->Instance->DIER &= ~(v))
#define __HAL_TIM_CLEAR_FLAG(t, v) ((t)->Instance->SR &= ~(v))
uint32_t HAL_GetTick(void);
void HAL_NVIC_SetPriority(int irq, uint32_t priority, uint32_t subpriority);
void HAL_NVIC_DisableIRQ(int irq);
void HAL_NVIC_EnableIRQ(int irq);
void HAL_NVIC_ClearPendingIRQ(int irq);
void HAL_RCC_GetClockConfig(RCC_ClkInitTypeDef *clocks, uint32_t *latency);
uint32_t HAL_RCC_GetPCLK1Freq(void);
int HAL_TIM_Base_Start(TIM_HandleTypeDef *timer);
int HAL_TIM_Base_Start_IT(TIM_HandleTypeDef *timer);
int HAL_TIM_Base_Stop_IT(TIM_HandleTypeDef *timer);
void HAL_TIM_PeriodElapsedCallback(TIM_HandleTypeDef *timer);
#endif
