#include "timebase.h"

#include <stdbool.h>

#include "tim.h"
#include "timebase_core.h"

static volatile uint32_t wraps;

void Timebase_Init(void)
{
    wraps = 0U;
    TIM2->SR = ~TIM_SR_UIF;
    HAL_NVIC_SetPriority(TIM2_IRQn, 4U, 0U);
    HAL_NVIC_ClearPendingIRQ(TIM2_IRQn);
    HAL_NVIC_EnableIRQ(TIM2_IRQn);
    (void)HAL_TIM_Base_Start_IT(&htim2);
}

void Timebase_UpdateIRQ(void)
{
    if ((TIM2->SR & TIM_SR_UIF) != 0U && (TIM2->DIER & TIM_DIER_UIE) != 0U)
    {
        TIM2->SR = ~TIM_SR_UIF;
        ++wraps;
    }
}

uint64_t Timebase_NowUs64(void)
{
    uint32_t before, counter, after;
    bool pending;
    uint64_t value;
    do
    {
        before = wraps;
        counter = TIM2->CNT;
        pending = (TIM2->SR & TIM_SR_UIF) != 0U;
        after = wraps;
    } while (!TimebaseCore_Compose(before, counter, pending, after, &value));
    return value;
}
